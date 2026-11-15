#!/bin/sh
set -e
umask 077

OPTIONS=/data/options.json
SESSION=/data/birdfy_session.json
MTX_CONFIG=/tmp/mediamtx.yml

echo "[birdfy] Starting Birdfy RTSP Proxy..."

# Remove plaintext session files left by the original app version, if any.
for old in /config/birdfy_session.json /config/birdfy_android_session.json; do
    if [ -f "$old" ]; then
        echo "[birdfy] Removing old session file $old"
        rm -f "$old"
    fi
done

# Login if there is no session yet (credentials are read from $OPTIONS inside Python).
if [ ! -f "$SESSION" ]; then
    echo "[birdfy] No session found, logging in..."
    python3 /app/birdfy_login.py --login --session "$SESSION"
fi

python3 /app/gen_mediamtx.py "$MTX_CONFIG" "$SESSION"

EVENTS=$(python3 -c "import json; print('1' if json.load(open('$OPTIONS')).get('events', True) else '0')")
if [ "$EVENTS" = "1" ]; then
    echo "[birdfy] Starting events daemon..."
    python3 /app/birdfy_events_daemon.py \
        --session "$SESSION" \
        --android-session /data/birdfy_android_session.json \
        --output /config/birdfy_events.json \
        --interval 300 &
fi

echo "[birdfy] Starting MediaMTX (on-demand mode)..."
exec mediamtx "$MTX_CONFIG"
