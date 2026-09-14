FROM python:3.11-slim-bookworm

LABEL version="0.4.0"
LABEL description="ShelfOps - slot-anchored CLIP shelf recognition, grid full-frame counting, per-item goods detection, stockout-restock state machine, heatmap analytics"

# HTTPS apt sources: some build networks block plain-HTTP egress (deb.debian.org:80)
RUN sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update && apt-get install -y --no-install-recommends \
    bash curl procps libglib2.0-0 libsm6 libxext6 libxrender1 \
    fonts-wqy-zenhei ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# SDK from PyPI, pinned via --build-arg SDK_VERSION by build_showcase_artifacts.sh;
# the default only matters for a bare `docker build` without the helper;
# the pinned source is sdk.lock at the repo root (NEORUNTIME_SDK_VERSION overrides).
ARG SDK_VERSION=0.7.4
RUN pip install --no-cache-dir "neoruntime-ipc-sdk==${SDK_VERSION}"

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Patch simple-websocket to disable permessage-deflate (client compat, same as model-showcase)
RUN sed -i 's/extensions=\[PerMessageDeflate()\]/extensions=[]/' \
    $(python3 -c "import simple_websocket.ws; print(simple_websocket.ws.__file__)")

COPY config.py clip_classify.py vocab.py slots.py analytics.py \
     alerts.py overlay.py detector.py media_import.py demo.py app.py /app/
COPY assets/ /app/assets/
COPY templates/ /app/templates/
COPY static/ /app/static/

# Bundled models (zoo clip via models.manifest + vendored yolo_world_v2s_540).
# NOT /opt/aipc/models — app.yaml bind-mounts host /data/aipc/models there,
# which would shadow image content on device.
COPY models/ /opt/aipc/bundled-models/

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
RUN mkdir -p /app/uploads && chown appuser:appuser /app/uploads

ENV APP_ID=shelf-ops
ENV PYTHONUNBUFFERED=1
ENV WEB_PORT=8891
ENV LOG_LEVEL=INFO

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${WEB_PORT}/api/health || exit 1

USER appuser
CMD ["python3", "/app/app.py"]