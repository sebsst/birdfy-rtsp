#!/bin/sh
set -e

OPTIONS=/data/options.json
SESSION=/config/birdfy_session.json

EMAIL=$(python3 -c "import json; print(json.loads(open('$OPTIONS').read())['email'])")
PASSWORD=$(python3 -c "import json; print(json.loads(open('$OPTIONS').read())['password'])")
RTSP_PORT=$(python3 -c "import json; print(json.loads(open('$OPTIONS').read()).get('rtsp_port', 8554))")

echo "[birdfy] Starting Birdfy RTSP Proxy..."

# Login if no session
if [ ! -f "$SESSION" ]; then
    echo "[birdfy] No session found, logging in..."
    python3 /app/birdfy_login.py --email "$EMAIL" --password "$PASSWORD" --session "$SESSION"
fi

# Write MediaMTX config with on-demand stream
cat > /tmp/mediamtx.yml << EOF
logLevel: warn
logDestinations: [stdout]
readTimeout: 60s
writeTimeout: 60s
writeQueueSize: 512
rtspAddress: :${RTSP_PORT}
rtspEncryption: "no"
rtspTransports: [tcp]
authInternalUsers:
- user: any
  pass:
  ips: []
  permissions:
  - action: publish
    path:
  - action: read
    path:
paths:
  birdfy:
    runOnDemand: python3 /app/birdfy_rtsp.py --session $SESSION --rtsp-url rtsp://localhost:${RTSP_PORT}/birdfy --no-turn
    runOnDemandCloseAfter: 10s
EOF

# Download mediamtx if not present
if [ ! -f /usr/local/bin/mediamtx ]; then
    echo "[birdfy] Downloading MediaMTX..."
    ARCH=$(uname -m)
    case "$ARCH" in
        aarch64) MTX_ARCH="linux_arm64v8" ;;
        armv7l)  MTX_ARCH="linux_armv7" ;;
        *)       MTX_ARCH="linux_amd64" ;;
    esac
    wget -q -O /tmp/mediamtx.tar.gz \
        "https://github.com/bluenviron/mediamtx/releases/download/v1.12.2/mediamtx_v1.12.2_${MTX_ARCH}.tar.gz"
    tar -xzf /tmp/mediamtx.tar.gz -C /usr/local/bin/ mediamtx
    chmod +x /usr/local/bin/mediamtx
fi

echo "[birdfy] Starting events daemon..."
python3 /app/birdfy_events_daemon.py \
    --session "$SESSION" \
    --android-session /config/birdfy_android_session.json \
    --output /config/birdfy_events.json \
    --email "$EMAIL" \
    --password "$PASSWORD" \
    --interval 300 &

echo "[birdfy] Starting MediaMTX on port ${RTSP_PORT} (on-demand mode)..."
exec mediamtx /tmp/mediamtx.yml
