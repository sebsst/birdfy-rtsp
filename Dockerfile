FROM alpine:3.20

# Install system dependencies
RUN apk add --no-cache \
    python3 \
    python3-dev \
    py3-pip \
    ffmpeg \
    gcc \
    musl-dev \
    libffi-dev \
    openssl-dev \
    opus-dev \
    libvpx-dev \
    && pip3 install --no-cache-dir --break-system-packages \
    aiortc>=1.6.0 \
    websockets>=11.0 \
    aiohttp>=3.8.0 \
    av>=10.0.0

# Copy addon files
WORKDIR /app
COPY birdfy_proxy.py .
COPY birdfy_rtsp.py .
COPY birdfy_login.py .
COPY birdfy_events_daemon.py .
COPY run.sh .
RUN chmod +x run.sh

CMD ["/app/run.sh"]
