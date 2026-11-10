# 05-live-detection — rung 5 of the examples ladder (capstone).
# Long-running web app: curl is present for the healthcheck; the model
# bundling matches rungs 03/04.

FROM python:3.11-slim-bookworm

LABEL maintainer="opensource@camthink.ai"
LABEL version="1.0.0"
LABEL description="NeoRuntime examples ladder 05 - live detection loop with web page"

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# SDK from PyPI, pinned via --build-arg SDK_VERSION by build_app.sh; the
# pinned source is sdk.lock at the repo root.
ARG SDK_VERSION=0.8.1
RUN pip install --no-cache-dir "neoruntime-ipc-sdk==${SDK_VERSION}"

COPY app.py /app/app.py

# Bundled model (fetched by scripts/fetch_models.sh into models/,
# sha256-pinned via models.manifest).
COPY models/ /opt/aipc/models/

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=INFO

# This rung IS a long-running web app: probe the status endpoint.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -sf http://127.0.0.1:8090/status || exit 1

CMD ["python3", "/app/app.py"]
