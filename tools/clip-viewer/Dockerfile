# CLIP Zero-Shot Viewer Application Container
# Lightweight — all NPU inference done by ai-runtime, this is just a web UI.
# For ARM64 (Hailo platform) with NeoRuntime SDK

FROM python:3.11-slim-bookworm

LABEL maintainer="opensource@camthink.ai"
LABEL version="2.0.0"
LABEL description="CLIP Zero-Shot Image Classification Viewer (NPU text encoding)"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    procps \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# SDK from PyPI (pinned via SDK_VERSION build-arg; default for bare docker build)
ARG SDK_VERSION=0.8.1
RUN pip install --no-cache-dir "neoruntime-ipc-sdk==${SDK_VERSION}"

# Copy application dependencies
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY app.py /app/app.py
COPY templates/ /app/templates/

# Create non-root user
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

# Environment variables (platform will override)
ENV APP_ID=clip-viewer
ENV PYTHONUNBUFFERED=1
ENV WEB_PORT=8888
ENV LOG_LEVEL=INFO

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:${WEB_PORT}/api/health || exit 1

# Switch to non-root user
USER appuser

# Run the application
CMD ["python3", "/app/app.py"]
