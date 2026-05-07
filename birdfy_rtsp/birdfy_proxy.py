#!/usr/bin/env python3
"""
Birdfy WebRTC → RTSP Proxy
===========================
Reproduit le protocole "vicoo/addx" de Netvue/Birdfy :
  1. WebSocket vers le serveur de signalisation
  2. Échange SDP offer/answer + ICE candidates  
  3. Message "startLive" sur le data channel
  4. Réception flux H264 via WebRTC (aiortc)
  5. Écriture vers fichier MP4 ou pipe RTSP

Modes:
  test   : connexion simple, affiche les logs
  record : enregistre N secondes dans un fichier MP4
  pipe   : pipe le flux vers stdout (pour ffmpeg)

Usage:
  python3 birdfy_proxy.py --access-token "eyJ..." --mode record --duration 10
  python3 birdfy_proxy.py --access-token "eyJ..." --mode pipe | ffplay -
"""

import argparse
import asyncio
import base64
import json
import logging
import re
import sys
import time
import uuid
from typing import Optional

import websockets
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCIceCandidate
from aiortc.contrib.media import MediaRecorder
from aiortc.sdp import candidate_from_sdp
from aiortc.codecs import CODECS
from aiortc.rtcrtpparameters import RTCRtpCodecParameters

# ── Patch aiortc : ajouter H264 f4001f (profile utilisé par Birdfy) ─────────
def _patch_aiortc_codecs():
    h264 = next((c for c in CODECS["video"] if c.name == "H264"), None)
    if h264 and not any(
        c.name == "H264" and c.parameters.get("profile-level-id") == "f4001f"
        for c in CODECS["video"]
    ):
        import copy
        h264_f4 = copy.deepcopy(h264)
        h264_f4.payloadType = 103
        h264_f4.parameters = {
            "level-asymmetry-allowed": "1",
            "packetization-mode": "1",
            "profile-level-id": "f4001f",
        }
        rtx_f4 = RTCRtpCodecParameters(
            mimeType="video/rtx",
            clockRate=90000,
            payloadType=104,
            parameters={"apt": 103},
            rtcpFeedback=[],
        )
        CODECS["video"] = [h264_f4, rtx_f4] + CODECS["video"]
        print("Codec H264 f4001f ajouté à aiortc")

_patch_aiortc_codecs()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("birdfy")

# ── Constantes protocole Netvue ─────────────────────────────────────────────
SIGNAL_HOST = "p-signal-262239938.smartvideogo.com"
GROUP_ID    = "79a6e62743ab26b9400097d4f7eb402a"
VIEWER_ID   = "362470"
MODE        = "vicoo"
VIEWER_TYPE = "a4x_sdk"


class BirdfyClient:
    """Reproduit exactement le comportement du navigateur."""

    def __init__(self, access_token: str = "", full_url: str = "", ice_servers: list = None,
                 on_rtp_packet=None):
        self.access_token   = access_token
        self._full_url      = full_url
        self._ice_servers   = ice_servers or []
        self.on_rtp_packet  = on_rtp_packet  # callback(data: bytes, addr) pour chaque paquet RTP vidéo
        self.pc: Optional[RTCPeerConnection] = None
        self.dc             = None
        self.session_id     = f"web-{VIEWER_ID}-{int(time.time()*1000)}"
        self.connection_id  = uuid.uuid4().hex
        self._video_track   = None
        self._audio_track   = None
        self._connected     = asyncio.Event()
        self._dc_open       = asyncio.Event()
        self._camera_video_ssrc: Optional[int] = None  # parsed from SDP_ANSWER a=ssrc:

    # ── URL WebSocket ────────────────────────────────────────────────────────
    def _wss_url(self) -> str:
        if self._full_url:
            return self._full_url
        trace = f"webrtc-{uuid.uuid4().hex[:12]}"
        ts    = int(time.time() * 1000)
        return (
            f"wss://{SIGNAL_HOST}/{GROUP_ID}/viewer/{VIEWER_ID}"
            f"?traceId={trace}&time={ts}"
            f"&accessToken={self.access_token}&name=a4x"
        )

    # ── Helpers encodage ─────────────────────────────────────────────────────
    @staticmethod
    def _b64enc(obj: dict) -> str:
        return base64.b64encode(json.dumps(obj).encode()).decode()

    @staticmethod
    def _b64dec(s: str) -> dict:
        return json.loads(base64.b64decode(s).decode())

    # ── Data channel ─────────────────────────────────────────────────────────
    def _setup_dc(self):
        @self.dc.on("open")
        def _():
            log.info("Data channel ouvert")
            self._dc_open.set()

        @self.dc.on("message")
        def _(msg):
            try:
                d = json.loads(msg)
                act = d.get("action", "?")
                rv  = d.get("returnValue", "")
                log.info(f"  DC ← {act}  rv={rv}  msg={str(d)[:200]}")
                if act == "startLive" and rv in (0, 17):
                    log.info("🎥 Stream actif !")
            except Exception:
                log.debug(f"DC raw: {msg[:80]}")

    # ── startLive ────────────────────────────────────────────────────────────
    async def _send_start_live(self):
        await self._dc_open.wait()
        msg = json.dumps({
            "requestId":    str(uuid.uuid4()),
            "connectionId": self.connection_id,
            "timeStamp":    int(time.time() * 1000),
            "action":       "startLive",
            "size":         "1920x1080",
            "resolution":   "auto",
            "mode":         MODE,
            "viewerType":   VIEWER_TYPE,
        })
        self.dc.send(msg)
        log.info(f"startLive → {msg[:80]}")
        # Lancer la boucle PLI en arrière-plan (demande IDR keyframe)
        asyncio.create_task(self._pli_loop(count=8, interval=1.5))

    # ── Connexion principale ─────────────────────────────────────────────────
    async def connect(self, on_track_cb=None):
        from aiortc import RTCConfiguration, RTCIceServer

        # Configurer les ICE servers TURN depuis la session
        ice_servers = []
        for srv in self._ice_servers:
            url = srv.get("url", "")
            # aiortc attend "turns:" ou "turn:" — normaliser
            ice_servers.append(RTCIceServer(
                urls=url,
                username=srv.get("username"),
                credential=srv.get("credential"),
            ))
        if ice_servers:
            log.info(f"ICE servers TURN configurés: {len(ice_servers)}")
            config = RTCConfiguration(iceServers=ice_servers)
        else:
            log.info("Pas d'ICE servers TURN — connexion directe LAN uniquement")
            config = RTCConfiguration()

        self.pc = RTCPeerConnection(configuration=config)
        self._patch_ice_for_dtls_sniff()
        self._silence_h264_decoder_warnings()

        # Transceivers recvonly (audio + video)
        self.pc.addTransceiver("audio", direction="recvonly")
        self.pc.addTransceiver("video", direction="recvonly")

        # Data channel
        self.dc = self.pc.createDataChannel("datachannel")
        self._setup_dc()

        # Tracks reçus
        @self.pc.on("track")
        def _(track):
            log.info(f"Track reçu : {track.kind}")
            if track.kind == "video":
                self._video_track = track
            else:
                self._audio_track = track
            if on_track_cb:
                on_track_cb(track)

        @self.pc.on("connectionstatechange")
        async def _():
            s = self.pc.connectionState
            log.info(f"PeerConnection → {s}")
            if s == "connected":
                self._connected.set()
                asyncio.create_task(self._send_start_live())
            elif s in ("failed", "closed", "disconnected"):
                self._connected.clear()

        @self.pc.on("icegatheringstatechange")
        def _():
            log.info(f"ICE gathering → {self.pc.iceGatheringState}")

        @self.pc.on("iceconnectionstatechange")
        def _():
            log.info(f"ICE connection → {self.pc.iceConnectionState}")

        # Étape 1 : créer l'offer aiortc pour obtenir ICE ufrag/pwd et fingerprint DTLS
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)

        # Attendre la fin du gathering (max 8s)
        deadline = time.time() + 8
        while self.pc.iceGatheringState != "complete" and time.time() < deadline:
            await asyncio.sleep(0.1)

        # Extraire les credentials ICE et le fingerprint DTLS de l'offer aiortc
        raw_sdp = self.pc.localDescription.sdp
        m = re.search(r"a=ice-ufrag:(\S+)", raw_sdp)
        ice_ufrag = m.group(1) if m else "xxx"
        m = re.search(r"a=ice-pwd:(\S+)", raw_sdp)
        ice_pwd = m.group(1) if m else "xxx"
        m = re.search(r"a=fingerprint:(sha-256 \S+)", raw_sdp)
        fingerprint = m.group(1) if m else "sha-256 00:00"

        # Extraire les candidates depuis le SDP gathered
        candidates_by_mid = {}
        mid_idx = -1
        current_mid = "0"
        for line in raw_sdp.splitlines():
            if line.startswith("m="):
                mid_idx += 1
                current_mid = str(mid_idx)
                candidates_by_mid.setdefault(current_mid, [])
            elif line.startswith("a=mid:"):
                current_mid = line[6:].strip()
                candidates_by_mid.setdefault(current_mid, [])
            elif line.startswith("a=candidate:"):
                candidates_by_mid.setdefault(current_mid, []).append(line)

        log.info(f"ICE credentials: ufrag={ice_ufrag} | fingerprint={fingerprint[:30]}...")
        log.info(f"Candidates par mid: { {k: len(v) for k, v in candidates_by_mid.items()} }")

        # Étape 2 : construire le browser_sdp (format attendu par la caméra Birdfy)
        # Utilise les credentials ICE d'aiortc + fingerprint DTLS d'aiortc
        # mais dans un format SDP simplifié / "navigateur"
        cands_0 = "\r\n".join(candidates_by_mid.get("0", []))
        cands_1 = "\r\n".join(candidates_by_mid.get("1", []))
        cands_2 = "\r\n".join(candidates_by_mid.get("2", []))
        if cands_0: cands_0 = cands_0 + "\r\n"
        if cands_1: cands_1 = cands_1 + "\r\n"
        if cands_2: cands_2 = cands_2 + "\r\n"

        ts = int(time.time() * 1000)
        browser_sdp = (
            f"v=0\r\n"
            f"o=- {ts} 2 IN IP4 127.0.0.1\r\n"
            f"s=-\r\n"
            f"t=0 0\r\n"
            f"a=group:BUNDLE 0 1 2\r\n"
            f"a=msid-semantic: WMS\r\n"
            f"m=audio 9 UDP/TLS/RTP/SAVPF 0\r\n"
            f"c=IN IP4 0.0.0.0\r\n"
            f"a=rtcp:9 IN IP4 0.0.0.0\r\n"
            f"a=ice-ufrag:{ice_ufrag}\r\n"
            f"a=ice-pwd:{ice_pwd}\r\n"
            f"a=ice-options:trickle\r\n"
            f"a=fingerprint:{fingerprint}\r\n"
            f"a=setup:actpass\r\n"
            f"a=mid:0\r\n"
            f"a=recvonly\r\n"
            f"a=rtcp-mux\r\n"
            f"a=rtcp-rsize\r\n"
            f"a=rtpmap:0 PCMU/8000\r\n"
            f"{cands_0}"
            f"m=video 9 UDP/TLS/RTP/SAVPF 103 104\r\n"
            f"c=IN IP4 0.0.0.0\r\n"
            f"a=rtcp:9 IN IP4 0.0.0.0\r\n"
            f"a=ice-ufrag:{ice_ufrag}\r\n"
            f"a=ice-pwd:{ice_pwd}\r\n"
            f"a=ice-options:trickle\r\n"
            f"a=fingerprint:{fingerprint}\r\n"
            f"a=setup:actpass\r\n"
            f"a=mid:1\r\n"
            f"a=recvonly\r\n"
            f"a=rtcp-mux\r\n"
            f"a=rtcp-rsize\r\n"
            f"a=rtpmap:103 H264/90000\r\n"
            f"a=rtcp-fb:103 goog-remb\r\n"
            f"a=rtcp-fb:103 transport-cc\r\n"
            f"a=fmtp:103 level-asymmetry-allowed=1;packetization-mode=1;profile-level-id=f4001f\r\n"
            f"a=rtpmap:104 rtx/90000\r\n"
            f"a=fmtp:104 apt=103\r\n"
            f"{cands_1}"
            f"m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
            f"c=IN IP4 0.0.0.0\r\n"
            f"a=ice-ufrag:{ice_ufrag}\r\n"
            f"a=ice-pwd:{ice_pwd}\r\n"
            f"a=ice-options:trickle\r\n"
            f"a=fingerprint:{fingerprint}\r\n"
            f"a=setup:actpass\r\n"
            f"a=mid:2\r\n"
            f"a=sctp-port:5000\r\n"
            f"a=max-message-size:262144\r\n"
            f"{cands_2}"
        )

        # Étape 3 : forcer aiortc à utiliser ce SDP (override local description)
        # Cela unifie les ICE credentials sur un seul ufrag pour le BUNDLE
        try:
            from aiortc import RTCSessionDescription as RSD
            await self.pc.setLocalDescription(RSD(sdp=browser_sdp, type="offer"))
            log.info("✅ browser_sdp appliqué comme localDescription")
        except Exception as e:
            log.warning(f"setLocalDescription(browser_sdp) échoué: {e} — on utilise le SDP brut")
            browser_sdp = raw_sdp

        setup_vals = re.findall(r"a=setup:(\S+)", browser_sdp)
        n_cands = browser_sdp.count("a=candidate:")
        log.info(f"SDP à envoyer: setup={setup_vals}, {n_cands} candidates inline")

        # Stocker le SDP pour l'envoyer après PEER_IN
        self._offer_sdp = browser_sdp

        # WebSocket
        url = self._wss_url()
        log.info(f"WS → {url[:90]}…")

        async with websockets.connect(
            url,
            additional_headers={
                "Origin":     "https://my.netvue.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
            ping_interval=20,
            ping_timeout=30,
            close_timeout=5,
            max_size=10_000_000,
        ) as ws:
            log.info("✅ WebSocket connecté — envoi SDP offer immédiat")
            await self._send_sdp_offer(ws)

            # Boucle messages
            async for raw in ws:
                await self._handle(ws, raw)

    async def _send_sdp_offer(self, ws):
        sdp_msg = {
            "messageType":       "SDP_OFFER",
            "recipientClientId": GROUP_ID,
            "senderClientId":    VIEWER_ID,
            "messagePayload":    self._b64enc({"sdp": self._offer_sdp, "type": "offer"}),
            "sessionId":         self.session_id,
            "resolution":        "auto",
            "viewerType":        VIEWER_TYPE,
            "mode":              MODE,
        }
        await ws.send(json.dumps(sdp_msg))
        log.info("→ SDP offer envoyé")

    # ── Traitement messages serveur ──────────────────────────────────────────
    async def _handle(self, ws, raw: str):
        log.info(f"←  serveur ({len(raw)}B): {raw[:300]}")
        try:
            msg = json.loads(raw)
        except Exception:
            return

        mtype = msg.get("messageType", "")

        if mtype == "PEER_IN":
            log.info("PEER_IN reçu — envoi SDP offer (retransmission)")
            await self._send_sdp_offer(ws)

        elif mtype == "SDP_ANSWER":
            # Ignorer si déjà en état stable (double answer après retransmission PEER_IN)
            if self.pc and self.pc.signalingState == "stable":
                log.info("SDP_ANSWER ignoré — signalingState déjà stable")
                return

            payload = self._b64dec(msg["messagePayload"])
            sdp_answer = payload.get("sdp", "")
            ans_type   = payload.get("type", "answer")

            cam_ufrag = re.search(r"a=ice-ufrag:(\S+)", sdp_answer)
            log.info(f"SDP answer reçu — ice-ufrag={cam_ufrag.group(1) if cam_ufrag else '?'}")
            # Parse camera's video SSRC from a=ssrc: lines in video section
            lines = sdp_answer.split("\r\n")
            in_vid = False
            vid_lines = []
            for line in lines:
                if line.startswith("m=video"):
                    in_vid = True
                if in_vid:
                    vid_lines.append(line)
                    m_ssrc = re.match(r"a=ssrc:(\d+)", line)
                    if m_ssrc and self._camera_video_ssrc is None:
                        self._camera_video_ssrc = int(m_ssrc.group(1))
                        log.info(f"Camera video SSRC: {self._camera_video_ssrc}")
                    if line.startswith("m=") and not line.startswith("m=video"):
                        in_vid = False
            log.info(f"=== VIDEO SDP_ANSWER ===\n" + "\n".join(vid_lines) + "\n=== END ===")
            # Full dump for debugging
            log.debug(f"=== FULL CAMERA SDP_ANSWER ===\n{sdp_answer}\n=== END ===")

            # Patch: audio sendrecv→sendonly (caméra envoie uniquement)
            patched = sdp_answer.replace("a=sendrecv\r\n", "a=sendonly\r\n")

            # Log camera setup values for debugging
            cam_setup = re.findall(r"a=setup:(\S+)", sdp_answer)
            log.info(f"Camera SDP_ANSWER setup values: {cam_setup}")

            try:
                ans = RTCSessionDescription(sdp=patched, type=ans_type)
                await self.pc.setRemoteDescription(ans)
                log.info("✅ SDP answer appliqué")
            except Exception as e:
                log.error(f"setRemoteDescription échoué: {e}")
                return

            # La caméra annonce a=setup:active ET envoie le ClientHello DTLS.
            # aiortc devient "server" (passive/wait) → correct.
            # Ne pas forcer le rôle client ici.

            await asyncio.sleep(0.5)
            await self._send_ice_candidates(ws)

        elif mtype == "ICE_CANDIDATE":
            payload = self._b64dec(msg["messagePayload"])
            cstr    = payload.get("candidate", "")
            mid     = payload.get("sdpMid", "0")
            idx     = payload.get("sdpMLineIndex", 0)
            if cstr and "candidate:" in cstr:
                try:
                    c = candidate_from_sdp(cstr.split("candidate:", 1)[1])
                    c.sdpMid        = mid
                    c.sdpMLineIndex = idx
                    await self.pc.addIceCandidate(c)
                    log.debug(f"  ICE ← {cstr[:60]}")
                except Exception as e:
                    log.debug(f"  ICE ignoré: {e}")

    # ── Envoi RTCP PLI (demande de keyframe) ─────────────────────────────────
    async def _send_rtcp_pli(self):
        """Envoie un RTCP PLI au receiver video pour demander un IDR keyframe."""
        try:
            for t in self.pc.getTransceivers():
                if t.kind == "video" and t.receiver:
                    recv = t.receiver
                    # Prefer SSRC parsed from SDP_ANSWER; fallback: first seen RTP SSRC
                    ssrc = self._camera_video_ssrc
                    if ssrc is None:
                        # Try to get from receiver's internal active streams
                        active = getattr(recv, '_RTCRtpReceiver__active_ssrc', {})
                        if active:
                            ssrc = next(iter(active))
                    if ssrc:
                        await recv._send_rtcp_pli(ssrc)
                        log.info(f"RTCP PLI envoyé ssrc={ssrc}")
                    else:
                        log.warning("RTCP PLI: ssrc inconnu (SDP_ANSWER n'avait pas a=ssrc:?)")
                    return
            log.warning("RTCP PLI: aucun transceiver video trouvé")
        except Exception as e:
            log.warning(f"RTCP PLI échoué: {e}")

    async def _pli_loop(self, count: int = 5, interval: float = 1.5):
        """Envoie plusieurs PLI à intervalles pour s'assurer de recevoir un IDR."""
        for i in range(count):
            await asyncio.sleep(interval)
            log.info(f"PLI #{i+1}/{count}")
            await self._send_rtcp_pli()

    # ── Debug + fix H264 decoder ─────────────────────────────────────────────
    def _silence_h264_decoder_warnings(self):
        """Suppress aiortc H264Decoder warnings — we bypass it entirely via ffmpeg pipe."""
        try:
            logging.getLogger("aiortc.codecs.h264").setLevel(logging.ERROR)
        except Exception:
            pass

    # ── Monkey-patch ICE pour logger les paquets non-STUN ────────────────────
    def _patch_ice_for_dtls_sniff(self):
        """Injecte un hook dans aioice pour logger tout paquet non-STUN reçu,
        et patche RTCDtlsTransport._handle_rtp_data pour intercepter les paquets
        RTP décryptés (post-SRTP) et les transmettre au callback on_rtp_packet."""
        # ── Patch 1 : logger les paquets non-STUN bruts (diagnostic uniquement) ──
        try:
            from aioice.ice import StunProtocol
            orig_stun = StunProtocol.datagram_received

            _dtls_logged = [False]
            def patched_stun(self_sp, data, addr):
                if len(data) > 0:
                    b0 = data[0]
                    if 0x14 <= b0 <= 0x17 and not _dtls_logged[0]:
                        log.debug(f"[ICE sniff] DTLS de {addr}: {len(data)}B")
                        _dtls_logged[0] = True
                return orig_stun(self_sp, data, addr)

            StunProtocol.datagram_received = patched_stun
        except Exception as e:
            log.warning(f"[ICE sniff] Impossible de patcher StunProtocol: {e}")

        # ── Patch 2 : intercepter RTP décrypté dans RTCDtlsTransport ─────────────
        if self.on_rtp_packet:
            try:
                from aiortc.rtcdtlstransport import RTCDtlsTransport
                orig_handle_rtp = RTCDtlsTransport._handle_rtp_data
                _self_ref = self
                _rtp_fwd_count = [0]
                _rtp_last_log  = [0.0]

                async def patched_handle_rtp(self_dt, data: bytes, arrival_time_ms: int):
                    # PT est dans les bits 0-6 de l'octet 1 (bit 7 = marker)
                    if len(data) > 1:
                        pt = data[1] & 0x7F
                        if pt == 103:  # H264 payload type Birdfy
                            try:
                                _self_ref.on_rtp_packet(data, None)
                            except Exception:
                                pass
                            _rtp_fwd_count[0] += 1
                            now = time.time()
                            if now - _rtp_last_log[0] >= 10:
                                log.info(f"[DTLS→RTP] {_rtp_fwd_count[0]} paquets H264 décryptés transmis")
                                _rtp_last_log[0] = now
                    return await orig_handle_rtp(self_dt, data, arrival_time_ms)

                RTCDtlsTransport._handle_rtp_data = patched_handle_rtp
                log.info("[DTLS→RTP] Patch RTCDtlsTransport._handle_rtp_data actif — RTP décrypté sera transmis")
            except Exception as e:
                log.warning(f"[DTLS→RTP] Impossible de patcher RTCDtlsTransport: {e}")

    # ── Force DTLS client role ────────────────────────────────────────────────
    def _force_dtls_client_role(self):
        """Force tous les DTLSTransport en rôle 'client' (initiateur DTLS).

        La caméra Birdfy déclare a=setup:active dans son SDP_ANSWER, ce qui
        ferait d'aiortc le serveur DTLS (passif, attend ClientHello). Mais la
        caméra n'envoie jamais de ClientHello — bug firmware. On renverse le
        rôle d'aiortc pour qu'il envoie le ClientHello en premier.
        """
        dtls_transports = set()
        for t in self.pc.getTransceivers():
            dt = t.receiver.transport
            if dt is not None:
                dtls_transports.add(dt)
        if self.pc.sctp and self.pc.sctp.transport is not None:
            dtls_transports.add(self.pc.sctp.transport)

        for dt in dtls_transports:
            old = dt._role
            dt._role = "client"
            log.info(f"DTLS role override: {old} → client  (transport={id(dt):#x})")

    # ── Envoi ICE candidates ─────────────────────────────────────────────────
    async def _send_ice_candidates(self, ws):
        """Attend la fin du ICE gathering puis envoie tous les candidates."""
        # Attendre que le gathering soit terminé (max 5s)
        deadline = time.time() + 10
        while self.pc.iceGatheringState != "complete" and time.time() < deadline:
            await asyncio.sleep(0.1)

        sdp = self.pc.localDescription.sdp if self.pc.localDescription else ""
        # Extraire candidates avec leur m-line index depuis le SDP
        sent = 0
        mid_idx = -1
        current_mid = "0"
        for line in sdp.splitlines():
            if line.startswith("m="):
                mid_idx += 1
                current_mid = str(mid_idx)
            elif line.startswith("a=mid:"):
                current_mid = line[6:].strip()
            elif line.startswith("a=candidate:"):
                cand_str = line[2:]  # "candidate:..."
                payload = {
                    "candidate":        cand_str,
                    "sdpMid":           current_mid,
                    "sdpMLineIndex":    mid_idx,
                    "usernameFragment": "",
                }
                ice_msg = {
                    "messageType":       "ICE_CANDIDATE",
                    "recipientClientId": GROUP_ID,
                    "senderClientId":    VIEWER_ID,
                    "sessionId":         self.session_id,
                    "messagePayload":    self._b64enc(payload),
                    "mode":              MODE,
                }
                await ws.send(json.dumps(ice_msg))
                log.debug(f"  ICE → mid={current_mid} {cand_str[:60]}")
                sent += 1

        log.info(f"Envoi de {sent} ICE candidates (gathering={self.pc.iceGatheringState})")

        # Signaler fin des candidates (end-of-candidates)
        for mid_v, idx_v in [("0", 0), ("1", 1), ("2", 2)]:
            eoc_msg = {
                "messageType":       "ICE_CANDIDATE",
                "recipientClientId": GROUP_ID,
                "senderClientId":    VIEWER_ID,
                "sessionId":         self.session_id,
                "messagePayload":    self._b64enc({
                    "candidate":        "",
                    "sdpMid":           mid_v,
                    "sdpMLineIndex":    idx_v,
                    "usernameFragment": "",
                }),
                "mode": MODE,
            }
            await ws.send(json.dumps(eoc_msg))
        log.info("End-of-candidates envoyé")


# ── Mode TEST ────────────────────────────────────────────────────────────────
async def run_test(token: str = "", timeout: int = 30, full_url: str = "", ice_servers: list = None):
    log.info("=== MODE TEST ===")
    client = BirdfyClient(access_token=token, full_url=full_url, ice_servers=ice_servers)
    try:
        await asyncio.wait_for(client.connect(), timeout=timeout)
    except asyncio.TimeoutError:
        log.info(f"Timeout {timeout}s atteint")
    except Exception as e:
        log.error(f"Erreur: {type(e).__name__}: {e}")
    finally:
        if client.pc:
            await client.pc.close()


# ── Mode RECORD : dump H264 Annex-B brut ─────────────────────────────────────
async def run_record(token: str = "", output: str = "birdfy_test.mp4", duration: int = 15, full_url: str = "", ice_servers: list = None):
    """Dump les frames H264 encodées (Annex-B) dans un fichier .h264 brut.

    Contourne le décodeur aiortc défaillant. Le fichier .h264 peut être lu
    directement par ffplay ou réencapsulé : ffmpeg -i out.h264 -c copy out.mp4
    """
    h264_output = output.replace(".mp4", ".h264").replace(".mkv", ".h264")
    if not h264_output.endswith(".h264"):
        h264_output += ".h264"
    log.info(f"=== MODE RECORD → {h264_output} (dump H264 Annex-B, {duration}s) ===")

    client = BirdfyClient(access_token=token, full_url=full_url, ice_servers=ice_servers)
    connect_task = asyncio.create_task(client.connect())

    try:
        await asyncio.wait_for(client._connected.wait(), timeout=90)
    except asyncio.TimeoutError:
        log.error("Timeout connexion WebRTC")
        connect_task.cancel()
        return

    log.info(f"Dump H264 pendant {duration}s → {h264_output}")
    ANNEX_B = b'\x00\x00\x00\x01'
    frame_count = 0
    bytes_written = 0

    with open(h264_output, "wb") as f:
        deadline = asyncio.get_event_loop().time() + duration
        video_track = client._video_track

        if video_track is None:
            log.error("Pas de video track reçu")
            connect_task.cancel()
            return

        # Intercepter les encoded frames depuis le receiver interne d'aiortc
        # aiortc expose les frames décodées via track.recv() — on veut les frames
        # encodées avant décodage. On patche le codec H264Decoder pour capturer
        # les données avant qu'elles soient décodées.
        captured_frames = []
        sps_seen = [False]  # n'écrire que ce qui vient après le 1er SPS
        from aiortc.codecs import h264 as h264_mod
        orig_decode = h264_mod.H264Decoder.decode

        def _contains_sps(data: bytes) -> bool:
            """Cherche un NAL type 7 (SPS) dans un paquet multi-NAL Annex-B."""
            i = 0
            while i < len(data) - 4:
                if data[i:i+4] == ANNEX_B:
                    nal = data[i+4] & 0x1f if i+4 < len(data) else 0
                    if nal == 7:
                        return True
                    i += 4
                elif data[i:i+3] == b'\x00\x00\x01':
                    nal = data[i+3] & 0x1f if i+3 < len(data) else 0
                    if nal == 7:
                        return True
                    i += 3
                else:
                    i += 1
            return False

        def capturing_decode(self_dec, encoded_frame):
            d = encoded_frame.data if encoded_frame.data else b""
            if d:
                # Assurer Annex-B start code
                if d[:4] != ANNEX_B:
                    d = ANNEX_B + d
                # Activer la capture dès qu'on voit un SPS (NAL 7) dans le paquet
                if not sps_seen[0] and _contains_sps(d):
                    sps_seen[0] = True
                    log.info(f"[record] Premier SPS détecté — début capture ({len(d)}B)")
                if sps_seen[0]:
                    captured_frames.append(d)
            return orig_decode(self_dec, encoded_frame)

        h264_mod.H264Decoder.decode = capturing_decode

        try:
            while asyncio.get_event_loop().time() < deadline:
                # Drainer les frames capturées
                while captured_frames:
                    data = captured_frames.pop(0)
                    f.write(data)
                    bytes_written += len(data)
                    frame_count += 1
                    if frame_count % 30 == 0:
                        log.info(f"  {frame_count} frames H264 dumpées ({bytes_written//1024}KB)")
                await asyncio.sleep(0.05)
        finally:
            h264_mod.H264Decoder.decode = orig_decode

    log.info(f"✅ Dump terminé: {frame_count} frames, {bytes_written//1024}KB → {h264_output}")
    log.info(f"   Lire avec: ffplay {h264_output}")
    log.info(f"   Convertir: ffmpeg -i {h264_output} -c copy birdfy.mp4")
    connect_task.cancel()
    if client.pc:
        await client.pc.close()


# ── Main ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Birdfy WebRTC Proxy")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument("--access-token", help="JWT accessToken WebRTC (eyJpZCI6...)")
    grp.add_argument("--wss-url",      help="URL WebSocket complète depuis les logs navigateur (wss://p-signal...)")
    grp.add_argument("--session",      help="Fichier birdfy_session.json généré par birdfy_login.py")
    p.add_argument("--mode",     choices=["test", "record"], default="test")
    p.add_argument("--output",   default="birdfy_test.mp4")
    p.add_argument("--duration", type=int, default=15)
    p.add_argument("--timeout",  type=int, default=90)
    p.add_argument("--debug",    action="store_true")
    p.add_argument("--no-turn",  action="store_true", help="Désactiver TURN (LAN direct uniquement)")
    args = p.parse_args()

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)
        # Always enable DTLS debug to diagnose role/handshake
    logging.getLogger("aiortc.rtcdtlstransport").setLevel(logging.DEBUG)

    token       = args.access_token or ""
    full_url    = args.wss_url or ""
    ice_servers = []

    # Charger depuis session.json si fourni
    if args.session:
        import os
        session_file = args.session
        if not os.path.exists(session_file):
            session_file = "birdfy_session.json"
        with open(session_file) as f:
            session = json.load(f)
        ticket = session.get("ticket")
        if ticket and ticket.get("wss_url"):
            full_url    = ticket["wss_url"]
            ice_servers = ticket.get("iceServer", [])
            log.info(f"Session chargée: {full_url[:80]}...")
            log.info(f"ICE servers depuis session: {[s.get('url') for s in ice_servers]}")
        else:
            log.error("Pas d'URL WSS dans la session. Lance d'abord birdfy_login.py")
            sys.exit(1)

    if getattr(args, "no_turn", False):
        ice_servers = []
        log.info("TURN désactivé — LAN direct uniquement")

    if args.mode == "test":
        asyncio.run(run_test(token=token, timeout=args.timeout, full_url=full_url, ice_servers=ice_servers))
    elif args.mode == "record":
        asyncio.run(run_record(token=token, output=args.output, duration=args.duration, full_url=full_url, ice_servers=ice_servers))


# ── Mode RECORD avec stats ────────────────────────────────────────────────────
async def run_record_v2(token: str = "", output: str = "birdfy.mp4", duration: int = 15, full_url: str = ""):
    """Version améliorée avec stats de réception."""
    log.info(f"=== MODE RECORD v2 → {output} ({duration}s) ===")
    client   = BirdfyClient(access_token=token, full_url=full_url)
    recorder = MediaRecorder(output)
    frame_counts = {"video": 0, "audio": 0}

    def on_track(track):
        log.info(f"Track ajouté au recorder: {track.kind}")
        recorder.addTrack(track)
        
        # Monkey-patch pour compter les frames
        original_recv = track.recv
        async def counting_recv():
            frame = await original_recv()
            frame_counts[track.kind] += 1
            if frame_counts[track.kind] % 30 == 0:
                log.info(f"  Frames reçues: video={frame_counts['video']} audio={frame_counts['audio']}")
            return frame
        track.recv = counting_recv

    connect_task = asyncio.create_task(client.connect(on_track_cb=on_track))

    try:
        await asyncio.wait_for(client._connected.wait(), timeout=45)
    except asyncio.TimeoutError:
        log.error("Timeout connexion WebRTC")
        connect_task.cancel()
        return

    log.info(f"Enregistrement {duration}s…")
    await recorder.start()
    await asyncio.sleep(duration)
    log.info(f"Stats finales: video={frame_counts['video']} audio={frame_counts['audio']}")
    await recorder.stop()
    log.info(f"✅ Enregistré : {output}")
    connect_task.cancel()
    if client.pc:
        await client.pc.close()
