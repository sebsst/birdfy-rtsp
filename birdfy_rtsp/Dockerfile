FROM alpine:3.20

ARG BUILD_ARCH=aarch64
ARG MEDIAMTX_VERSION=v1.12.2

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
    libvpx-dev

# Pinned versions (the versions current when the original app was released;
# birdfy_proxy.py patches aiortc internals, so newer aiortc may break it).
RUN pip3 install --no-cache-dir --break-system-packages \
    aiortc==1.14.0 \
    av==17.0.1 \
    aiohttp==3.13.5 \
    websockets==16.0

# MediaMTX: downloaded at build time and verified by SHA-256 checksum.
RUN set -e; \
    case "$BUILD_ARCH" in \
        aarch64) MTX_ARCH=linux_arm64; MTX_SHA=35803953e27a7b242efb1f25b4d48e3cc24999bcb43f6895383a85d6f8000651 ;; \
        amd64)   MTX_ARCH=linux_amd64; MTX_SHA=f0ec6e21c3cde41d02f186f58636f0ea8ee67c9d44dacf5b9391e85600f56e74 ;; \
        armv7)   MTX_ARCH=linux_armv7; MTX_SHA=b10a5267113bc013339e3bfc7b60a3c32aeba1bf56f0e86be36f02b123ff1328 ;; \
        armhf)   MTX_ARCH=linux_armv6; MTX_SHA=765156e430b6664d1092116e33c5ba5c5fc711d0fe4a0e5805326852d0fa7523 ;; \
        *) echo "Unsupported arch: $BUILD_ARCH"; exit 1 ;; \
    esac; \
    wget -q -O /tmp/mediamtx.tar.gz \
        "https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_${MTX_ARCH}.tar.gz"; \
    echo "${MTX_SHA}  /tmp/mediamtx.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/mediamtx.tar.gz -C /usr/local/bin/ mediamtx; \
    chmod +x /usr/local/bin/mediamtx; \
    rm /tmp/mediamtx.tar.gz

# Copy addon files
WORKDIR /app
COPY secure_store.py gen_mediamtx.py birdfy_proxy.py birdfy_rtsp.py birdfy_login.py birdfy_events_daemon.py run.sh ./
RUN chmod +x run.sh

CMD ["/app/run.sh"]
