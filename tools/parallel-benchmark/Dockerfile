FROM python:3.11-slim-bookworm

LABEL version="1.0.0"
LABEL description="NeoRuntime Parallel Benchmark - Model Parallel Capability Showcase"

RUN apt-get update && apt-get install -y --no-install-recommends \
    bash curl procps libglib2.0-0 libsm6 libxext6 libxrender1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# SDK from PyPI (pinned via SDK_VERSION build-arg; default for bare docker build)
ARG SDK_VERSION=0.8.1
RUN pip install --no-cache-dir "neoruntime-ipc-sdk==${SDK_VERSION}"

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY parallel_benchmark/ /app/parallel_benchmark/
COPY templates/ /app/templates/
COPY app.yaml /app/

RUN mkdir -p /app/logs /app/data

RUN useradd -m -u 1000 appuser && chown -R appuser:appuser /app

ENV APP_ID=parallel_benchmark
ENV PYTHONUNBUFFERED=1
ENV WEB_PORT=8095
ENV DEBUG=0

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:${WEB_PORT}/api/health || exit 1

USER appuser
CMD ["python3", "-m", "parallel_benchmark.app"]
