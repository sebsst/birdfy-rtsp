# Birdfy RTSP — Home Assistant Addon

Home Assistant addon that streams a [Birdfy](https://www.birdfy.com/) camera as an RTSP stream via WebRTC.

The stream starts on demand when a client connects, and stops automatically when no one is watching — preserving camera battery.

## Installation

1. In Home Assistant, go to **Settings → Add-ons → Add-on Store**
2. Click the three-dot menu → **Repositories**
3. Add `https://github.com/sebsst/birdfy-rtsp`
4. Install **Birdfy RTSP**

## Configuration

| Option | Description |
|--------|-------------|
| `email` | Netvue/Birdfy account email |
| `password` | Netvue/Birdfy account password |
| `rtsp_port` | RTSP port (default: 8556) |
| `rtsp_username` / `rtsp_password` | Optional login required to read the RTSP stream |
| `use_turn` | Use the TURN relay servers from the WebRTC ticket (default: off) |
| `events` | Run the events daemon that writes `/config/birdfy_events.json` (default: on) |

## Home Assistant camera setup

```yaml
# configuration.yaml
camera:
  - platform: generic
    name: Birdfy
    stream_source: rtsp://192.168.1.45:8556/birdfy
    verify_ssl: false
```

## Architecture

```
Birdfy camera
    → WebRTC/SRTP (Netvue protocol)
    → birdfy_proxy.py (Python aiortc)
    → H264 Annex-B pipe
    → ffmpeg (-c:v copy)
    → MediaMTX (on-demand)
    → rtsp://<ha>:8556/birdfy
    → Home Assistant
```

## Notes

- Requires `host_network: true` — needed for WebRTC ICE to work from the container
- The camera firmware forces TURN relay — direct LAN ICE is not supported
- For SD card recordings and event browsing, see [birdfy-integration](https://github.com/sebsst/birdfy-integration)

## Security

- The account password is only kept in the app options; it is never written to the session file, command lines, environment variables or the MediaMTX config.
- Session files live in the app's private `/data` folder with `0600` permissions.
- Tokens and login responses are not written to the log.
- MediaMTX only enables RTSP; publishing is allowed from localhost only.
