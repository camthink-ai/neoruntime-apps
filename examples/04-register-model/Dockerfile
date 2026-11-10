# 04-register-model — rung 4 of the examples ladder.
# Same model bundling as rung 03 (diff the Dockerfiles: identical apart
# from labels) — the difference is WHO registers it: app-manager
# (spec.models) there, the app itself (register_model) here.

FROM python:3.11-slim-bookworm

LABEL maintainer="opensource@camthink.ai"
LABEL version="1.0.0"
LABEL description="NeoRuntime examples ladder 04 - runtime model registration"

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

# One-shot app: no long-running process to probe.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -m compileall -q /app || exit 1

CMD ["python3", "/app/app.py"]
