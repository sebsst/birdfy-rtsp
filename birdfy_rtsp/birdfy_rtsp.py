#!/usr/bin/env python3
"""
Birdfy WebRTC -> RTSP via MediaMTX
===================================
Pipeline:
  Camera -> WebRTC/SRTP -> Python -> H264 Annex-B pipe -> ffmpeg -> RTSP -> MediaMTX -> HA

Prerequis:
  1. Lancer MediaMTX:  mediamtx_bin/mediamtx.exe mediamtx_birdfy.yml
  2. Generer session:  python birdfy_login.py --email xxx --password yyy
  3. Lancer:           python birdfy_rtsp.py --session birdfy_session.json

Home Assistant: rtsp://IP_PC:8554/birdfy
"""

import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from birdfy_proxy import BirdfyClient, _patch_aiortc_codecs, log
from birdfy_login import refresh as refresh_ticket

_patch_aiortc_codecs()

MEDIAMTX_URL      = "rtsp://localhost:8554/birdfy"
RECONNECT_DELAY_BASE = 30
RECONNECT_DELAY_MAX  = 300

ANNEX_B = b'\x00\x00\x00\x01'


def start_ffmpeg(rtsp_url: str) -> subprocess.Popen:
    """Read raw H264 Annex-B from stdin, re-encode at steady 15fps, publish to RTSP."""
    cmd = [
        "ffmpeg", "-y",
        "-loglevel", "warning",
        # Input: raw H264 Annex-B bitstream from stdin
        "-f", "h264",
        "-i", "pipe:0",
        # Re-encode to fix timestamps and smooth output
        "-c:v", "copy",
        "-flush_packets", "1",
        "-f", "rtsp",
        "-rtsp_transport", "tcp",
        rtsp_url,
    ]
    log.info(f"[ffmpeg] Lancement -> {rtsp_url}")
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=False,
        bufsize=0,
    )


class RtpH264Depacketizer:
    """
    Reassemble RTP/H264 packets (RFC 6184) into Annex-B NAL units.
    Handles single NAL, STAP-A, and FU-A. Drops incomplete FU-A on gap.
    """
    def __init__(self, on_nal):
        self._on_nal     = on_nal
        self._fu_buf     = bytearray()
        self._fu_started = False
        self._last_seq   = None

    def feed(self, rtp_payload: bytes, seq: int):
        # Detect sequence gap — discard in-progress FU-A fragment
        if self._last_seq is not None:
            expected = (self._last_seq + 1) & 0xFFFF
            if seq != expected:
                self._fu_buf     = bytearray()
                self._fu_started = False
        self._last_seq = seq

        if not rtp_payload:
            return
        nal_type = rtp_payload[0] & 0x1F

        if nal_type <= 23:
            self._on_nal(bytes(rtp_payload))

        elif nal_type == 24:
            # STAP-A
            i = 1
            while i + 2 <= len(rtp_payload):
                size = (rtp_payload[i] << 8) | rtp_payload[i+1]
                i += 2
                if i + size > len(rtp_payload):
                    break
                self._on_nal(bytes(rtp_payload[i:i+size]))
                i += size

        elif nal_type == 28:
            # FU-A
            if len(rtp_payload) < 2:
                return
            fu_header = rtp_payload[1]
            start   = (fu_header >> 7) & 1
            end     = (fu_header >> 6) & 1
            nal_hdr = (rtp_payload[0] & 0xE0) | (fu_header & 0x1F)

            if start:
                self._fu_buf     = bytearray([nal_hdr])
                self._fu_started = True

            if self._fu_started:
                self._fu_buf += rtp_payload[2:]

            if end and self._fu_started:
                self._on_nal(bytes(self._fu_buf))
                self._fu_buf     = bytearray()
                self._fu_started = False


async def one_session(session_file: str, rtsp_url: str, no_turn: bool) -> bool:
    """Single WebRTC session. Returns True if we connected (even briefly)."""

    try:
        await refresh_ticket(session_file)
    except Exception as e:
        log.warning(f"Refresh ticket echoue: {e} -- tentative re-login")
        email    = os.environ.get("BIRDFY_EMAIL", "")
        password = os.environ.get("BIRDFY_PASSWORD", "")
        if email and password:
            from birdfy_login import main as login_main
            log.info("Re-login avec les credentials de l'environnement...")
            try:
                await login_main(email, password, session_file=session_file)
            except Exception as e2:
                log.error(f"Re-login echoue: {e2}")
        else:
            log.warning("Pas de credentials disponibles — on reutilise l'ancien ticket")

    with open(session_file) as f:
        session = json.load(f)
    ticket = session.get("ticket") or {}
    full_url         = ticket.get("wss_url", "")
    ice_servers      = [] if no_turn else ticket.get("iceServer", [])
    ping_interval    = ticket.get("signalPingInterval", 2)

    if not full_url:
        log.error("Pas d'URL WSS dans la session")
        return False

    ffmpeg_proc = start_ffmpeg(rtsp_url)

    async def read_ffmpeg():
        loop = asyncio.get_event_loop()
        while ffmpeg_proc.poll() is None:
            line = await loop.run_in_executor(None, ffmpeg_proc.stdout.readline)
            if line:
                log.info(f"[ffmpeg] {line.rstrip().decode(errors='replace')}")
            else:
                await asyncio.sleep(0.1)

    ffmpeg_reader = asyncio.create_task(read_ffmpeg())

    _fwd_count = [0]
    _last_log  = [0.0]
    loop = asyncio.get_event_loop()

    def on_nal(nal: bytes):
        """Write one complete NAL unit (Annex-B) to ffmpeg stdin."""
        try:
            ffmpeg_proc.stdin.write(ANNEX_B + nal)
            ffmpeg_proc.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    depacketizer = RtpH264Depacketizer(on_nal)

    def on_rtp_packet(data: bytes, addr):
        if len(data) < 12:
            return
        pt = data[1] & 0x7F
        if pt != 103:
            return
        # Parse RTP header to find payload offset
        cc      = data[0] & 0x0F          # CSRC count
        has_ext = (data[0] >> 4) & 0x01   # extension bit
        offset  = 12 + cc * 4
        if has_ext and len(data) >= offset + 4:
            ext_len = int.from_bytes(data[offset+2:offset+4], 'big')
            offset += 4 + ext_len * 4
        payload = data[offset:]
        seq = (data[2] << 8) | data[3]
        depacketizer.feed(payload, seq)
        _fwd_count[0] += 1
        now = time.time()
        if now - _last_log[0] >= 10:
            log.info(f"[pipe] {_fwd_count[0]} NAL -> ffmpeg stdin")
            _last_log[0] = now

    client = BirdfyClient(full_url=full_url, ice_servers=ice_servers, on_rtp_packet=on_rtp_packet,
                          ping_interval=ping_interval)
    connect_task = asyncio.create_task(client.connect())

    try:
        await asyncio.wait_for(client._connected.wait(), timeout=90)
    except asyncio.TimeoutError:
        log.warning("Timeout connexion WebRTC (90s)")
        connect_task.cancel()
        ffmpeg_proc.terminate()
        ffmpeg_reader.cancel()
        return False

    log.info(f"WebRTC connecte -- RTSP disponible: {rtsp_url}")

    try:
        while True:
            await asyncio.sleep(5)
            state = client.pc.connectionState if client.pc else "closed"
            if state in ("failed", "closed", "disconnected"):
                log.warning(f"WebRTC perdu ({state}) -- reconnexion")
                return True
            if ffmpeg_proc.poll() is not None:
                log.warning(f"ffmpeg arrete (code {ffmpeg_proc.returncode}) -- reconnexion")
                return True
    except asyncio.CancelledError:
        raise
    finally:
        ffmpeg_reader.cancel()
        try:
            ffmpeg_proc.stdin.close()
        except Exception:
            pass
        try:
            ffmpeg_proc.terminate()
        except Exception:
            pass
        connect_task.cancel()
        if client.pc:
            await client.pc.close()
        log.info(f"Session terminee -- {_fwd_count[0]} paquets transmis")


async def run_rtsp_loop(session_file: str, rtsp_url: str, no_turn: bool):
    attempt = 0
    consecutive_failures = 0
    try:
        while True:
            attempt += 1
            log.info(f"=== Tentative #{attempt} ===")
            try:
                connected = await one_session(session_file, rtsp_url, no_turn)
            except KeyboardInterrupt:
                break
            except Exception as e:
                log.error(f"Erreur inattendue: {e}", exc_info=True)
                connected = False

            if connected:
                consecutive_failures = 0
                delay = RECONNECT_DELAY_BASE
            else:
                consecutive_failures += 1
                delay = min(RECONNECT_DELAY_BASE * (2 ** (consecutive_failures - 1)), RECONNECT_DELAY_MAX)

            log.info(f"Prochaine tentative dans {delay}s...")
            await asyncio.sleep(delay)
    except KeyboardInterrupt:
        pass
    log.info("Arret")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%H:%M:%S",
    )

    p = argparse.ArgumentParser(description="Birdfy WebRTC -> RTSP via MediaMTX")
    p.add_argument("--session",  required=True, help="birdfy_session.json")
    p.add_argument("--rtsp-url", default=MEDIAMTX_URL)
    p.add_argument("--no-turn",  action="store_true")
    p.add_argument("--debug",    action="store_true")
    args = p.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    asyncio.run(run_rtsp_loop(
        session_file=args.session,
        rtsp_url=args.rtsp_url,
        no_turn=args.no_turn,
    ))
