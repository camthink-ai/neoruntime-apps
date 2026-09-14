# Person Detection Application Container
# For ARM64 (Hailo platform) with NeoRuntime SDK

FROM python:3.11-slim-bookworm

LABEL maintainer="opensource@camthink.ai"
LABEL version="1.0.0"
LABEL description="Person Detection Application with NeoRuntime SDK"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    bash \
    curl \
    procps \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    && rm -rf /var/lib/apt/lists/*

# Create app directory
WORKDIR /app

# SDK from PyPI, pinned via --build-arg SDK_VERSION by build_app.sh; the
# default only matters for a bare `docker build` without the helper; the
# pinned source is sdk.lock at the repo root (NEORUNTIME_SDK_VERSION overrides).
ARG SDK_VERSION=0.7.4
RUN pip install --no-cache-dir "neoruntime-ipc-sdk==${SDK_VERSION}"

# Copy application files
COPY app.py /app/app.py
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# Bundled models (fetched by scripts/fetch_models.sh into models/, sha256-pinned)
COPY models/ /opt/aipc/models/

# Create non-root user
RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /app

# Create data and logs directories
RUN mkdir -p /app/data /app/logs && \
    chown -R appuser:appuser /app/data /app/logs

# Environment variables (platform will override)
ENV APP_ID=person-detection
ENV PYTHONUNBUFFERED=1
ENV LOG_LEVEL=INFO

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "from neoruntime_ipc_sdk import InferenceClient; c = InferenceClient(); c.close()" || exit 1

# Switch to non-root user
USER appuser

# Run the application
CMD ["python3", "/app/app.py"]
