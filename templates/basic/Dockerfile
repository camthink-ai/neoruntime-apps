# NeoRuntime Application Container Template
# Standalone image: the NeoRuntime SDK is installed from PyPI (pinned via the
# SDK_VERSION build-arg; the default only matters for a bare `docker build`).

FROM python:3.11-slim-bookworm

LABEL maintainer="you@example.com"
LABEL version="1.0.0"
LABEL description="NeoRuntime Application Template"

# Install additional system dependencies (if needed)
# RUN apt-get update && apt-get install -y --no-install-recommends \
#     libglib2.0-0 libsm6 libxext6 \
#     && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# NeoRuntime SDK from PyPI
ARG SDK_VERSION=0.8.1
RUN pip install --no-cache-dir "neoruntime-ipc-sdk==${SDK_VERSION}"

# Copy application files
COPY app.py app.yaml /app/

# App-specific dependencies: add requirements.txt and uncomment
# COPY requirements.txt /app/
# RUN pip install --no-cache-dir -r requirements.txt

# Create data directories
RUN mkdir -p /app/logs /app/data

# Environment variables (will be overridden by app-manager)
ENV APP_ID=template_app
ENV DEBUG=0
ENV LOG_LEVEL=INFO

# Health check - validates SDK connectivity
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "from neoruntime_ipc_sdk import AppClient; c = AppClient(); c.close()"

CMD ["python3", "app.py"]
