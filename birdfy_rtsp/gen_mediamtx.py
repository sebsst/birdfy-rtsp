#!/usr/bin/env python3
"""
Generate the MediaMTX config from the app options.

- Only the RTSP server is enabled (RTMP/HLS/WebRTC/SRT/API are off), so no
  extra ports are opened on the host network.
- Publishing to the stream is allowed only from this machine (127.0.0.1).
- Reading the stream needs a username/password when rtsp_username and
  rtsp_password are set in the options.
- The Birdfy account credentials are NOT written into this file.

YAML is a superset of JSON, so the config is written as JSON to get safe
quoting of any special characters.
"""
import json
import os
import re
import sys

OPTIONS_FILE = os.environ.get("BIRDFY_OPTIONS", "/data/options.json")


# Characters MediaMTX accepts in plain credentials (needed for Digest auth).
_ALLOWED = re.compile(r"^[A-Za-z0-9!$()*+.;<=>\[\]^_\-,@#&]+$")


def main(out_path: str, session_file: str) -> None:
    with open(OPTIONS_FILE) as f:
        opts = json.load(f)

    port = int(opts.get("rtsp_port", 8556))
    rtsp_user = (opts.get("rtsp_username") or "").strip()
    rtsp_pass = opts.get("rtsp_password") or ""
    use_turn = bool(opts.get("use_turn", False))

    if rtsp_user and rtsp_pass:
        for label, value in (("rtsp_username", rtsp_user), ("rtsp_password", rtsp_pass)):
            if not _ALLOWED.match(value):
                sys.exit(f"[birdfy] {label} may only contain letters, digits and "
                         "! $ ( ) * + . ; < = > [ ] ^ _ - , @ # &  (no spaces, colons or quotes)")
        reader = {"user": rtsp_user, "pass": rtsp_pass, "ips": [],
                  "permissions": [{"action": "read", "path": "birdfy"}]}
    else:
        reader = {"user": "any", "pass": "", "ips": [],
                  "permissions": [{"action": "read", "path": "birdfy"}]}

    publisher = {"user": "any", "pass": "", "ips": ["127.0.0.1/32", "::1/128"],
                 "permissions": [{"action": "publish", "path": "birdfy"}]}

    cmd = (f"python3 /app/birdfy_rtsp.py --session {session_file} "
           f"--rtsp-url rtsp://127.0.0.1:{port}/birdfy")
    if not use_turn:
        cmd += " --no-turn"

    config = {
        "logLevel": "warn",
        "logDestinations": ["stdout"],
        "readTimeout": "60s",
        "writeTimeout": "60s",
        "writeQueueSize": 512,
        "authMethod": "internal",
        "authInternalUsers": [publisher, reader],
        "api": False,
        "metrics": False,
        "pprof": False,
        "playback": False,
        "rtsp": True,
        "rtspAddress": f":{port}",
        "rtspEncryption": "no",
        "rtspTransports": ["tcp"],
        "rtmp": False,
        "hls": False,
        "webrtc": False,
        "srt": False,
        "paths": {
            "birdfy": {
                "runOnDemand": cmd,
                "runOnDemandCloseAfter": "30s",
            }
        },
    }

    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
