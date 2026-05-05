#!/usr/bin/env python3
"""
Daemon: polls Birdfy events every POLL_INTERVAL seconds and writes
/config/birdfy_events.json for Home Assistant to consume.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import aiohttp

UCID = "b3cf543b57"
UDID = "android-10aa8cf1-d060-4333-b738-f541f07b65ae"
API_BASE  = "https://eu-central-1-api2.nvts.co"
LOGIN_URL = "https://localweb.nvts.co/v1/users/login/v2"

POLL_INTERVAL = 300  # seconds
MAX_EVENTS    = 10

logging.basicConfig(
    level=logging.INFO,
    format="[birdfy-events] %(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def hmac_sha256(key: bytes, msg: str) -> str:
    if isinstance(key, str):
        key = key.encode()
    return hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()


def make_signature(token: str, userid: str, ts: str) -> str:
    k = "nvs1" + token
    for msg in [UCID, UDID, userid, ts]:
        k = hmac_sha256(k.encode(), msg)
    return hmac_sha256(k.encode(), "nvs1_request")


def auth_headers(token: str, userid: str) -> dict:
    ts = str(int(time.time() * 1000))
    sig = make_signature(token, userid, ts)
    return {
        "Accept": "application/json",
        "Accept-Charset": "UTF-8",
        "Accept-Encoding": "gzip",
        "User-Agent": "Birdfy/1.19.2 (build 123960) NetvueSDK/1.6.1 Android/12",
        "x-nvs-signature": sig,
        "x-nvs-time": ts,
        "x-nvs-ucid": UCID,
        "x-nvs-udid": UDID,
        "x-nvs-userid": userid,
        "x-nvs-version": '{"signature":2}',
    }


async def android_login(email: str, password: str) -> dict:
    pwd_md5 = hashlib.md5(password.encode()).hexdigest()
    payload = {"username": email, "password": pwd_md5, "locale": "en-US"}
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-nvs-ucid": UCID,
        "x-nvs-udid": UDID,
        "User-Agent": "Birdfy/1.19.2 (build 123960) NetvueSDK/1.6.1 Android/12",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(LOGIN_URL, json=payload, headers=headers) as r:
            data = await r.json(content_type=None)
            if data.get("ret", 0) != 0:
                raise RuntimeError(f"Login failed: {data}")
            return data


async def get_events(token: str, userid: str, device_id: str) -> list:
    url = f"{API_BASE}/devices/{device_id}/events"
    params = {"limit": MAX_EVENTS, "ignoreAiLabels": "false", "reverse": 1}
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=auth_headers(token, userid), params=params) as r:
            if r.status == 401:
                raise PermissionError("Token expired")
            data = await r.json(content_type=None)
            return data.get("events", [])


async def get_image_url(token: str, userid: str, alarm_id: str, device_id: str) -> str:
    url = f"{API_BASE}/devices/{device_id}/events/{alarm_id}/pic"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=auth_headers(token, userid)) as r:
            if r.status == 200:
                data = await r.json(content_type=None)
                return data.get("url", "")
            return ""


def parse_event(ev: dict) -> dict:
    alarm_id   = ev.get("alarmId", "")
    alert_time = ev.get("alertTime", 0)
    label      = ev.get("label", "")
    desc_raw   = ev.get("description", "{}")
    record_url = ""
    try:
        desc = json.loads(desc_raw)
        record_url = desc.get("recordUrl", "")
    except Exception:
        pass
    return {
        "alarm_id":   alarm_id,
        "alert_time": alert_time,
        "label":      label,
        "record_url": record_url,
    }


async def load_session(session_file: str, android_session_file: str, email: str, password: str):
    """Return (token, userid, device_id), re-logging in if needed."""
    token = userid = ""

    if os.path.exists(android_session_file):
        with open(android_session_file) as f:
            s = json.load(f)
        if s.get("token") and s.get("userID"):
            token  = s["token"]
            userid = str(s["userID"])

    if not token and email and password:
        log.info("Android login...")
        s = await android_login(email, password)
        with open(android_session_file, "w") as f:
            json.dump(s, f, indent=2)
        token  = s["token"]
        userid = str(s["userID"])

    with open(session_file) as f:
        web = json.load(f)
    devices = web.get("devices", [])
    if not devices:
        raise RuntimeError("No devices in session file")
    device_id = (
        devices[0].get("serialNumber")
        or devices[0].get("deviceSn")
        or devices[0].get("sn")
        or devices[0].get("addxSn")
    )
    return token, userid, device_id


async def poll_once(token: str, userid: str, device_id: str, output_file: str,
                    email: str, password: str, android_session_file: str,
                    session_file: str) -> tuple[str, str]:
    """Fetch events and write JSON. Returns (token, userid) possibly refreshed."""
    try:
        events_raw = await get_events(token, userid, device_id)
    except PermissionError:
        log.warning("Token expired, re-logging in...")
        if os.path.exists(android_session_file):
            os.remove(android_session_file)
        token, userid, _ = await load_session(session_file, android_session_file, email, password)
        events_raw = await get_events(token, userid, device_id)

    events = [parse_event(e) for e in events_raw]

    # Fetch image URL for most recent event only (to avoid spamming the API)
    if events and not events[0]["record_url"]:
        events[0]["image_url"] = await get_image_url(token, userid, events[0]["alarm_id"], device_id)
    elif events:
        events[0]["image_url"] = await get_image_url(token, userid, events[0]["alarm_id"], device_id)

    last = events[0] if events else {}
    output = {
        "last_updated":     int(time.time()),
        "last_event":       last,
        "recent_events":    events,
    }

    tmp = output_file + ".tmp"
    with open(tmp, "w") as f:
        json.dump(output, f, indent=2)
    os.replace(tmp, output_file)
    log.info(f"Wrote {len(events)} events → {output_file}  (last: {last.get('label','?')} @ {last.get('alert_time','')})")
    return token, userid


async def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--session",          default="/config/birdfy_session.json")
    p.add_argument("--android-session",  default="/config/birdfy_android_session.json")
    p.add_argument("--output",           default="/config/birdfy_events.json")
    p.add_argument("--email",            default=os.environ.get("BIRDFY_EMAIL", ""))
    p.add_argument("--password",         default=os.environ.get("BIRDFY_PASSWORD", ""))
    p.add_argument("--interval",         type=int, default=POLL_INTERVAL)
    args = p.parse_args()

    token, userid, device_id = await load_session(
        args.session, args.android_session, args.email, args.password
    )
    log.info(f"Device: {device_id}  user: {userid}")

    while True:
        try:
            token, userid = await poll_once(
                token, userid, device_id,
                args.output, args.email, args.password,
                args.android_session, args.session,
            )
        except Exception as e:
            log.error(f"Poll error: {e}")
        await asyncio.sleep(args.interval)


if __name__ == "__main__":
    asyncio.run(main())
