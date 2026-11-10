# Visualizer Application
# Subscribe to inference events and draw on video frames

FROM python:3.10-slim

LABEL maintainer="NeoRuntime Team"
LABEL description="Inference result visualizer"
LABEL version="1.0.0"

# Install OpenCV dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# SDK from PyPI (pinned via SDK_VERSION build-arg; default for bare docker build)
ARG SDK_VERSION=0.8.1
RUN pip install --no-cache-dir "neoruntime-ipc-sdk==${SDK_VERSION}"

# Copy application
COPY main.py .

# Create output directory
RUN mkdir -p /app/output

# Run
CMD ["python", "-u", "main.py"]
