FROM python:3.10-slim

WORKDIR /app

# SDK from PyPI, pinned via --build-arg SDK_VERSION by build_app.sh; the
# default only matters for a bare `docker build` without the helper; the
# pinned source is sdk.lock at the repo root (NEORUNTIME_SDK_VERSION overrides).
ARG SDK_VERSION=0.7.4
RUN pip install --no-cache-dir "neoruntime-ipc-sdk==${SDK_VERSION}"

COPY app.py .

# Bundled models (fetched by scripts/fetch_models.sh into models/, sha256-pinned)
COPY models/ /opt/aipc/models/

CMD ["python", "-u", "app.py"]
