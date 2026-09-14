FROM python:3.11-slim-bookworm

LABEL version="1.2.0"
LABEL description="Parking Lot Full Element Extraction: vehicles, anti-spoofing, plate OCR"

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl procps libglib2.0-0 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# SDK from PyPI, pinned via --build-arg SDK_VERSION by build_showcase_artifacts.sh;
# the default only matters for a bare `docker build` without the helper;
# the pinned source is sdk.lock at the repo root (NEORUNTIME_SDK_VERSION overrides).
ARG SDK_VERSION=0.7.4
RUN pip install --no-cache-dir "neoruntime-ipc-sdk==${SDK_VERSION}"

COPY requirements.txt /app/
RUN pip install --no-cache-dir --retries 5 --timeout 120 -r requirements.txt

COPY parking_lot/ /app/parking_lot/
COPY main.py /app/main.py
COPY templates/ /app/templates/
COPY static/ /app/static/
# Flask resolves templates/static relative to the package (__name__=parking_lot.web)
# so also place them under the package directory for Jinja2 to find them.
COPY templates/ /app/parking_lot/templates/
COPY static/ /app/parking_lot/static/

# Bundled models (fetch_models.sh + vendored device artifacts, see models.manifest)
COPY models/ /opt/aipc/models/

RUN mkdir -p /app/uploads && useradd -m -u 1000 appuser \
    && chown -R appuser:appuser /app

ENV APP_ID=parking_lot
ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=INFO
ENV WEB_PORT=8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -f http://localhost:${WEB_PORT}/api/health || exit 1

USER appuser
CMD ["python3", "/app/main.py"]
