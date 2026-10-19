# SDK Teaching Demo — interactive teaching app image. Unlike the pure
# A-form app this one ALSO runs the B-form path (subscribe frames,
# InferencePipeline, draw_detections), so opencv is a real dependency:
# opencv-python-headless covers cv2 without libGL.
# Zero-pixel claim applies to the A-form station and the platform
# overlay path, not to the whole app.

FROM python:3.11-slim-bookworm

# SDK from PyPI, pinned via --build-arg SDK_VERSION by the builder; the
# default only matters for a bare `docker build` without the helper; the
# pinned source is sdk.lock at the repo root (NEORUNTIME_SDK_VERSION overrides).
ARG SDK_VERSION=0.8.1
RUN pip install --no-cache-dir \
    "neoruntime-ipc-sdk==${SDK_VERSION}" \
    "opencv-python-headless" \
    "numpy"

WORKDIR /app
COPY *.py /app/
COPY templates/ /app/templates/
COPY models/ /opt/aipc/bundled-models/

RUN useradd -m -u 1000 appuser

ENV APP_ID=sdk-teaching-demo
ENV HTTP_PORT=8090
ENV PYTHONUNBUFFERED=1

# The app's own face is its HTTP API; probe it (stdlib only, no curl).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python3 -c "import os,urllib.request; urllib.request.urlopen('http://127.0.0.1:%s/api/health' % os.environ.get('HTTP_PORT','8090'), timeout=3)" || exit 1

USER appuser
CMD ["python3", "/app/main.py"]
