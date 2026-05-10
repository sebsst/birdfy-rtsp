#!/usr/bin/env python3
"""
Login automatique Netvue pour obtenir le token WebRTC sans navigateur.
Usage: python birdfy_login.py --email xxx --password yyy
"""
import asyncio
import aiohttp
import hashlib
import hmac
import time
import json
import argparse

# Constantes API Netvue (identiques à l'intégration HA)
UCID = "b3cf543b57"
UDID = f"android-{__import__('uuid').uuid4()}"

LOGIN_URL  = "https://localweb.nvts.co/v1/users/login/v2"
DEVICE_URL = "https://localweb.nvts.co/v1/devices/v3"
TOKEN_URL  = "https://api2.nvts.co/addx/token/v2"

LOGIN_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "Birdfy/1.19.2 (build 123960) NetvueSDK/1.6.1 Android/12",
    "x-nvs-ucid": UCID,
    "x-nvs-udid": UDID,
}

def md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()

def hmac_sha256(key: bytes, msg: str) -> str:
    if isinstance(key, str):
        key = key.encode()
    return hmac.new(key, msg.encode(), hashlib.sha256).hexdigest()

def make_signature(token: str, userid: str, ts: str) -> str:
    # La clé à chaque étape est le hex string (pas les bytes décodés)
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

async def login(email: str, password: str) -> dict:
    payload = {
        "username": email,
        "password": md5(password),
        "locale": "fr-FR",
        "platform": 0,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(LOGIN_URL, json=payload, headers=LOGIN_HEADERS) as r:
            data = await r.json(content_type=None)
            print(f"Login status: {r.status}")
            print(f"Login response: {json.dumps(data, indent=2)[:800]}")
            return data

async def get_devices(token: str, userid: str, local_endpoint: str = "https://localweb.nvts.co") -> list:
    url = f"https://localweb.nvts.co/v1/devices/v3"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=auth_headers(token, userid)) as r:
            data = await r.json(content_type=None)
            print(f"Devices response: {json.dumps(data, indent=2)[:400]}")
            return data.get("devices", data.get("data", {}).get("deviceList", data.get("deviceList", [])))

async def get_webrtc_token(token: str, userid: str, region: str = "eu-central-1") -> dict:
    url = f"{TOKEN_URL}?region={region}&forceUpdate=true"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=auth_headers(token, userid)) as r:
            return await r.json(content_type=None)
_WEB_APP_BLOCK = {
    "bundle": "com.netviewtech.mynetvue",
    "channelId": 1000,
    "appBuild": "online-build",
    "appName": "Netvue",
    "tenantId": "netvue",
    "countlyId": "",
    "version": 99999,
    "appType": "iOS",
}

def _addx_headers(addx_bearer: str, region: str) -> dict:
    return {
        "Accept": "application/json",
        "Accept-Charset": "UTF-8",
        "Accept-Encoding": "gzip",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/146.0.0.0 Safari/537.36",
        "x-nvs-a4x-region": region,
        "Authorization": addx_bearer,
    }

async def select_single_device(addx_bearer: str, addx_sn: str, endpoint: str = "https://api-eu.vicohome.io", region: str = "eu-central-1") -> dict:
    """Vérifie l'état de la caméra (équivalent au wakeup dans le flow web)."""
    import uuid
    url = f"{endpoint}/device/selectsingledevice"
    body = {
        "requestId": str(uuid.uuid4()),
        "language": "fr",
        "countryNo": "FR",
        "serialNumber": addx_sn,
        "app": _WEB_APP_BLOCK,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=body, headers=_addx_headers(addx_bearer, region)) as r:
            data = await r.json(content_type=None)
            awake = data.get("data", {}).get("awake") if data.get("result") == 0 else None
            print(f"selectsingledevice status={r.status} result={data.get('result')} awake={awake}")
            return data

async def wakeup_device(addx_bearer: str, addx_sn: str, endpoint: str = "https://api-eu.vicohome.io", region: str = "eu-central-1") -> None:
    """Réveille la caméra avant de demander le ticket WebRTC."""
    import uuid
    url = f"{endpoint}/device/wakeupDevice"
    body = {
        "requestId": str(uuid.uuid4()),
        "language": "fr",
        "serialNumber": addx_sn,
        "app": _WEB_APP_BLOCK,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=body, headers=_addx_headers(addx_bearer, region)) as r:
            data = await r.json(content_type=None)
            print(f"wakeupDevice status={r.status} result={data.get('result')} msg={data.get('msg')}")


async def get_webrtc_ticket(addx_bearer: str, addx_sn: str, endpoint: str = "https://api-eu.vicohome.io", region: str = "eu-central-1") -> dict:
    import uuid
    url = f"{endpoint}/device/getWebrtcTicket"
    body = {
        "requestId": str(uuid.uuid4()),
        "language": "fr",
        "countryNo": "FR",
        "serialNumber": addx_sn,
        "verifyDormancyStatus": True,
        "app": _WEB_APP_BLOCK,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=body, headers=_addx_headers(addx_bearer, region)) as r:
            return await r.json(content_type=None)

async def main(email: str, password: str, **kwargs):
    print(f"[1] Login {email}...")
    login_data = await login(email, password)

    # La réponse contient directement le token sans champ "code"
    token  = login_data.get("token")
    userid = str(login_data.get("userID") or login_data.get("userId", ""))
    region = login_data.get("region", "eu-central-1")
    local_endpoint = login_data.get("localEndpoint", "https://localweb.nvts.co")

    if not token:
        print(f"❌ Login échoué: pas de token dans la réponse")
        return None

    print(f"✅ Login OK — userid={userid} region={region} token={token[:40]}...")

    print(f"\n[2] Récupération des appareils ({local_endpoint})...")
    devices = await get_devices(token, userid, local_endpoint)
    for d in devices:
        print(f"  - {d.get('deviceName','?')} id={d.get('deviceId','?')} group={d.get('groupId','?')}")

    print(f"\n[3] Token WebRTC (région {region})...")
    rtc_data = await get_webrtc_token(token, userid, region)
    print(f"WebRTC: {json.dumps(rtc_data, indent=2)[:600]}")

    # [4] Ticket WebRTC (sign + time + ICE servers)
    ticket = None
    if devices:
        dev = devices[0]
        addx_sn = dev.get("addxSn")
        addx_bearer = rtc_data.get("token", "")
        endpoint = rtc_data.get("endpoint", "https://api-eu.vicohome.io")
        if addx_sn and addx_bearer:
            print(f"\n[4] Vérification état caméra addxSn={addx_sn}...")
            dev_info = await select_single_device(addx_bearer, addx_sn, endpoint, region)
            if dev_info.get("data", {}).get("awake") != 1:
                print(f"\n[4b] Caméra endormie — réveil forcé...")
                await wakeup_device(addx_bearer, addx_sn, endpoint, region)
                await asyncio.sleep(2)
            print(f"\n[5] Ticket WebRTC pour addxSn={addx_sn}...")
            ticket_resp = await get_webrtc_ticket(addx_bearer, addx_sn, endpoint, region)
            print(f"Ticket: {json.dumps(ticket_resp, indent=2)[:600]}")
            ticket = ticket_resp.get("data")
            if ticket:
                sign_raw = ticket.get("sign", "")
                if "&accessToken=" in sign_raw:
                    sign_part = sign_raw.split("&accessToken=")[0]
                    access_jwt = sign_raw.split("&accessToken=")[1]
                else:
                    sign_part = sign_raw
                    access_jwt = ticket.get("accessToken", "")
                t = ticket.get("time")
                trace_id = ticket.get("traceId", "webrtc-python")
                signal_server = ticket.get("signalServer", "")
                gid = ticket.get("groupId", addx_sn)
                vid = ticket.get("id", "")
                wss_url = (f"{signal_server}/{gid}/viewer/{vid}"
                           f"?traceId={trace_id}&time={t}&sign={sign_part}"
                           f"&accessToken={access_jwt}&name=a4x")
                print(f"\n✅ URL WSS:\n{wss_url[:300]}")
                ticket["wss_url"] = wss_url
                ticket["signalPingInterval"] = ticket.get("signalPingInterval", 2)

    result = {
        "token": token,
        "userid": userid,
        "ucid": UCID,
        "udid": UDID,
        "region": region,
        "local_endpoint": local_endpoint,
        "devices": devices,
        "webrtc": rtc_data,
        "ticket": ticket,
        "email": kwargs.get("email", email),
        "password": kwargs.get("password", password),
    }
    session_file = kwargs.get("session_file", "birdfy_session.json")
    with open(session_file, "w") as f:
        json.dump(result, f, indent=2)
    print(f"\nSession sauvegardee dans {session_file}")
    return result

async def refresh(session_file: str = "birdfy_session.json"):
    """Rafraîchit le ticket WebRTC sans re-login (réutilise le token existant)."""
    with open(session_file) as f:
        saved = json.load(f)

    token  = saved.get("token", "")
    userid = str(saved.get("userid", ""))
    region = saved.get("region", "eu-central-1")
    local_endpoint = saved.get("local_endpoint", "https://localweb.nvts.co")

    if not token:
        print("❌ Pas de token dans la session — lance d'abord avec --email/--password")
        return

    print(f"[token] Réutilisation token={token[:40]}... userid={userid}")

    print(f"\n[3] Token WebRTC (région {region})...")
    rtc_data = await get_webrtc_token(token, userid, region)
    print(f"WebRTC: {json.dumps(rtc_data, indent=2)[:400]}")

    if rtc_data.get("ret") == "113" or not rtc_data.get("token"):
        # Token expiré — re-login automatique si les credentials sont disponibles
        email    = saved.get("email", "")
        password = saved.get("password", "")
        if email and password:
            print("[token] Token expiré — re-login automatique...")
            result = await main(email, password, session_file=session_file)
            if result:
                saved["ticket"] = result.get("ticket")
                with open(session_file, "w") as f:
                    json.dump(result, f, indent=2)
            return
        raise Exception(f"Token Netvue expiré ({rtc_data.get('msg')}) — relancer avec --email/--password")

    devices = saved.get("devices", [])
    ticket = None
    if devices:
        dev = devices[0]
        addx_sn = dev.get("addxSn")
        addx_bearer = rtc_data.get("token", "")
        endpoint = rtc_data.get("endpoint", "https://api-eu.vicohome.io")
        if addx_sn and addx_bearer:
            print(f"\n[4] Vérification état caméra addxSn={addx_sn}...")
            dev_info = await select_single_device(addx_bearer, addx_sn, endpoint, region)
            if dev_info.get("data", {}).get("awake") != 1:
                print(f"\n[4b] Caméra endormie — réveil forcé...")
                await wakeup_device(addx_bearer, addx_sn, endpoint, region)
                await asyncio.sleep(2)
            print(f"\n[5] Ticket WebRTC pour addxSn={addx_sn}...")
            ticket_resp = await get_webrtc_ticket(addx_bearer, addx_sn, endpoint, region)
            print(f"Ticket: {json.dumps(ticket_resp, indent=2)[:400]}")
            ticket = ticket_resp.get("data")
            if ticket:
                sign_raw = ticket.get("sign", "")
                if "&accessToken=" in sign_raw:
                    sign_part = sign_raw.split("&accessToken=")[0]
                    access_jwt = sign_raw.split("&accessToken=")[1]
                else:
                    sign_part = sign_raw
                    access_jwt = ticket.get("accessToken", "")
                t = ticket.get("time")
                trace_id = ticket.get("traceId", "webrtc-python")
                signal_server = ticket.get("signalServer", "")
                gid = ticket.get("groupId", addx_sn)
                vid = ticket.get("id", "")
                wss_url = (f"{signal_server}/{gid}/viewer/{vid}"
                           f"?traceId={trace_id}&time={t}&sign={sign_part}"
                           f"&accessToken={access_jwt}&name=a4x")
                print(f"\nURL WSS:\n{wss_url[:300]}")
                ticket["wss_url"] = wss_url
                ticket["signalPingInterval"] = ticket.get("signalPingInterval", 2)

    saved["webrtc"] = rtc_data
    saved["ticket"] = ticket
    with open(session_file, "w") as f:
        json.dump(saved, f, indent=2)
    print("\nSession rafraichie dans", session_file)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--email")
    p.add_argument("--password")
    p.add_argument("--refresh", action="store_true", help="Rafraîchir le ticket sans re-login")
    p.add_argument("--session", default="birdfy_session.json")
    args = p.parse_args()

    if args.refresh:
        asyncio.run(refresh(args.session))
    elif args.email and args.password:
        asyncio.run(main(args.email, args.password, session_file=args.session, email=args.email, password=args.password))
    else:
        p.error("Fournir --email et --password, ou --refresh pour réutiliser la session existante")
