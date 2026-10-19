# 01-hello-app — rung 1 of the examples ladder.
# Base image and SDK pin match the other rungs so `diff` between rungs
# stays meaningful; the app itself only touches the SDK's offline
# Config helper (no sockets).

FROM python:3.11-slim-bookworm

LABEL maintainer="opensource@camthink.ai"
LABEL version="1.0.0"
LABEL description="NeoRuntime examples ladder 01 - app lifecycle"

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# SDK from PyPI, pinned via --build-arg SDK_VERSION by build_app.sh; the
# default only matters for a bare `docker build` without the helper; the
# pinned source is sdk.lock at the repo root (NEORUNTIME_SDK_VERSION
# overrides).
ARG SDK_VERSION=0.8.1
RUN pip install --no-cache-dir "neoruntime-ipc-sdk==${SDK_VERSION}"

COPY app.py /app/app.py

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app
USER appuser

ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=INFO

# No device daemons to probe in this rung — liveness = sources still
# byte-compile.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -m compileall -q /app || exit 1

CMD ["python3", "/app/app.py"]
