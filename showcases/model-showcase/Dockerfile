FROM python:3.11-slim-bookworm

LABEL version="1.4.4"
LABEL description="AI Model Showcase - Dynamic model discovery, RTSP streaming support"

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl procps libglib2.0-0 libsm6 libxext6 libxrender1 \
    fonts-wqy-zenhei ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Download mediamtx RTSP server (lightweight, single binary)
ARG TARGETARCH=arm64
RUN set -e; \
    case "${TARGETARCH}" in \
      arm64) MEDIAMTX_ARCH="arm64v8" ;; \
      amd64) MEDIAMTX_ARCH="amd64" ;; \
      *) MEDIAMTX_ARCH="arm64v8" ;; \
    esac; \
    curl -fsSL "https://github.com/bluenviron/mediamtx/releases/download/v1.11.3/mediamtx_v1.11.3_linux_${MEDIAMTX_ARCH}.tar.gz" \
    | tar xz -C /usr/local/bin/ mediamtx; \
    chmod +x /usr/local/bin/mediamtx

WORKDIR /app

# SDK from PyPI, pinned via --build-arg SDK_VERSION by build_showcase_artifacts.sh;
# the default only matters for a bare `docker build` without the helper;
# the pinned source is sdk.lock at the repo root (NEORUNTIME_SDK_VERSION overrides).
ARG SDK_VERSION=0.7.4
RUN pip install --no-cache-dir "neoruntime-ipc-sdk==${SDK_VERSION}"

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Patch simple-websocket to disable permessage-deflate (causes client compat issues)
RUN sed -i 's/extensions=\[PerMessageDeflate()\]/extensions=[]/' \
    $(python3 -c "import simple_websocket.ws; print(simple_websocket.ws.__file__)")

COPY main.py /app/main.py
COPY mediamtx.yml /app/mediamtx.yml
COPY ocr_dict.txt /app/ocr_dict.txt
COPY templates/ /app/templates/
COPY static/ /app/static/

# Bundled models (zoo rows via fetch_models.sh + vendored device artifacts).
# NOT /opt/aipc/models — app.yaml bind-mounts the host model store there,
# which would shadow image content.
COPY models/ /opt/aipc/bundled-models/

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app

ENV APP_ID=model-showcase
ENV PYTHONUNBUFFERED=1
ENV WEB_PORT=8889
ENV LOG_LEVEL=INFO

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${WEB_PORT}/api/health || exit 1

USER appuser
CMD ["python3", "/app/main.py"]
