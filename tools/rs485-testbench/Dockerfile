# RS-485 Testbench - bench tool for the platform RS-485 RX/TX path.
FROM python:3.11-slim-bookworm

LABEL version="0.1.0"
LABEL description="NeoRuntime RS-485 Testbench - RX/TX bench tool with a web console"

WORKDIR /app

# NeoRuntime SDK from PyPI. The build arg (sdk.lock, or NEORUNTIME_SDK_VERSION,
# see scripts/build_app.sh) acts as a lower bound: this tool calls
# DeviceClient.subscribe_rs485_rx(), which first shipped in 0.8.2, so an older
# pin must never select a release without it.
ARG SDK_VERSION=0.8.2
RUN pip install --no-cache-dir "neoruntime-ipc-sdk>=${SDK_VERSION},>=0.8.2"

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY rs485_testbench/ /app/rs485_testbench/
COPY templates/ /app/templates/
COPY app.py app.yaml /app/

RUN mkdir -p /app/logs /app/data \
    && useradd -m -u 1000 appuser \
    && chown -R appuser:appuser /app

ENV APP_ID=rs485_testbench
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV WEB_PORT=8097

# Liveness: the console answers its own health probe (no curl in the image).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python3", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('WEB_PORT', '8097') + '/api/health', timeout=3)"]

USER appuser
CMD ["python3", "/app/app.py"]
