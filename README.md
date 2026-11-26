# NeoRuntime Apps

[![Build showcase bundles](https://github.com/camthink-ai/neoruntime-apps/actions/workflows/showcase-artifacts.yml/badge.svg)](https://github.com/camthink-ai/neoruntime-apps/actions/workflows/showcase-artifacts.yml)

Official sample applications and templates for the NeoRuntime edge AI platform.

These applications are built against the NeoRuntime SDK and packaged as
containerized NeoRuntime apps.

## Downloads

| Showcase | Latest ARM64 bundle |
| -------- | ------------------- |
| Model Showcase | [model-showcase-latest-arm64.neoapp](https://github.com/camthink-ai/neoruntime-apps/releases/download/showcase-bundles-latest/model-showcase-latest-arm64.neoapp) |
| Parking Lot | [parking-lot-latest-arm64.neoapp](https://github.com/camthink-ai/neoruntime-apps/releases/download/showcase-bundles-latest/parking-lot-latest-arm64.neoapp) |
| Gym Ops | [gym-ops-latest-arm64.neoapp](https://github.com/camthink-ai/neoruntime-apps/releases/download/showcase-bundles-latest/gym-ops-latest-arm64.neoapp) |
| Shelf Ops | [shelf-ops-latest-arm64.neoapp](https://github.com/camthink-ai/neoruntime-apps/releases/download/showcase-bundles-latest/shelf-ops-latest-arm64.neoapp) |
| SDK Teaching Demo | [sdk-teaching-demo-latest-arm64.neoapp](https://github.com/camthink-ai/neoruntime-apps/releases/download/showcase-bundles-latest/sdk-teaching-demo-latest-arm64.neoapp) |

[All releases](https://github.com/camthink-ai/neoruntime-apps/releases)

## Repository Layout

- `templates/` - reusable app templates and manifest references
- `examples/` - small apps that demonstrate one platform capability at a time
- `tools/` - developer utilities, diagnostics, and plugin examples
- `showcases/` - richer end-to-end reference applications

## App Index

| Path | Type | Description |
| ---- | ---- | ----------- |
| `templates/basic/` | Template | Minimal app template and manifest reference |
| `examples/hello-world/` | Example | Basic lifecycle and logging example |
| `examples/person-detection/` | Example | Person detection with inference and events |
| `examples/people-counting/` | Example | People-counting starter application |
| `examples/object-detection/` | Example | Object detection and tracking starter |
| `tools/clip-viewer/` | Tool | CLIP zero-shot image classification viewer |
| `tools/visualizer/` | Tool | Real-time inference result preview |
| `tools/parallel-benchmark/` | Tool | Parallel inference benchmark dashboard |
| `tools/plugin-rtsp/` | Tool | C++ RTSP plugin example |
| `tools/rs485-testbench/` | Tool | RS-485 RX/TX bench tool with a web console and a PC peer script |
| `showcases/model-showcase/` | Showcase | Multi-model web showcase with visualization |
| `showcases/parking-lot/` | Showcase | Parking-lot vehicle and plate extraction reference |
| `showcases/gym-ops/` | Showcase | Gym pose coaching and occupancy reference |
| `showcases/shelf-ops/` | Showcase | Shelf slot recognition: slot-anchored CLIP A–E classification, empty-slot & sales heatmap reference |
| `showcases/sdk-teaching-demo/` | Showcase | Interactive single-page SDK teaching app: five live stations (B-form loop, overlay paths, routing, A-form, events) with the real code behind each one |

## Development

Install the SDK from PyPI, then work inside one app directory at a time:

```bash
cd examples/person-detection
python -m venv .venv
. .venv/bin/activate
pip install neoruntime-ipc-sdk
pip install -r requirements.txt
python app.py
```

Most apps include a `build.sh` script that packages a container image and
application manifest for deployment to an NeoRuntime device. Images install
`neoruntime-ipc-sdk==<version>` from PyPI inside the Dockerfile; the version
is pinned by `sdk.lock` at the repo root (reproducible, offline-friendly).
Set `NEORUNTIME_SDK_VERSION=<version>` to build against a different release, or
`NEORUNTIME_SDK_VERSION=latest` to float to the newest PyPI release. A weekly
GitHub Actions probe canaries new SDK releases and opens a bump PR. When such a
bump passes a version `showcases/sdk-teaching-demo` has been verified against,
its CI test step fails with instructions — re-verify the demo (offline suite +
device e2e), then extend `SUPPORTED_SDK_VERSIONS` in `sdk_helpers.py`.

Runtime credentials, device addresses, and generated `.neoapp` packages are
intentionally not committed. Use environment variables and local deployment
configuration for device-specific values. Model HEFs *are* committed (vendored
per app via `.gitignore` negations) — see [Model Files](#model-files).

Every example and showcase also builds in CI (arm64 via QEMU): examples build
and pass an in-image smoke test (`scripts/smoke_image.sh`) on every change;
showcases produce downloadable bundles — see [Showcase Bundles](#showcase-bundles).

### Model Files

App images bundle their model dependencies so installs are plug-and-play on
fresh devices — no manual `/data/aipc/models` provisioning step.

- Each app lists its downloadable models in `models.manifest` (TSV:
  `sha256<TAB>filename<TAB>url`). `scripts/fetch_models.sh <app-dir>`
  downloads and verifies them into `<app-dir>/models/`; `build_app.sh` and
  the showcase bundle build run it automatically.
- HEFs with no public source (recompiled inputs, custom class sets) are
  vendored directly in `<app-dir>/models/` and committed via `.gitignore`
  negations. All current apps — examples included — commit their HEFs this
  way, so a clean clone builds fully offline; `models.manifest` still pins
  the sha256 of every downloaded HEF so `fetch_models.sh` can verify or
  refresh them from the zoo.
- Dockerfiles copy `models/` into the image. Apps whose `app.yaml`
  bind-mounts the host model directory must copy to
  `/opt/aipc/bundled-models` instead of `/opt/aipc/models`, or the mount
  shadows the image content on device.
- Apps resolve model paths host-first (`MODEL_ROOT`), falling back to the
  bundled copy — devices that pre-provision models keep using their own.
- `spec.models` entries that auto-register a model at install time are only
  safe for models whose raw output needs no postprocess (embedding, depth)
  or whose app re-registers it with a variant. See `docs/app-permissions.md`.

## Showcase Bundles

Showcase bundles are `.neoapp` packages: tar.gz archives holding `app.yaml`,
`image.tar` (a `docker save` of the app image), companion YAML files, and
`SHA256SUMS`, all under a `<showcase>-<version>-<arch>/` directory. Every
`build.sh` in this repo produces the same package format, so examples and
showcases install identically. GitHub Actions builds these bundles when
showcase files or the build scripts change, and tag builds attach the
bundles to the GitHub Release.

To build locally, run the build script from any showcase directory (it wraps
`scripts/build_showcase_artifacts.sh`):

```bash
cd showcases/shelf-ops
./build.sh
```

The Python SDK (`neoruntime-ipc-sdk`) is installed from PyPI during the image
build, so a clean clone needs no sibling SDK repositories or tokens (version
pinned by `sdk.lock`; `NEORUNTIME_SDK_VERSION` overrides). Model HEFs are committed
per showcase and verified against `models.manifest` at build time, so installed
bundles run on fresh devices; device-provisioned copies still take precedence
where present.

To install a downloaded bundle on a device, upload the `.neoapp` file in the
web app import dialog, or extract it and run:

```bash
tar xzf <showcase>-<version>-arm64.neoapp
cd <showcase>-<version>-arm64
aipc-cli app install app.yaml image.tar
```

## Related Repositories

- `camthink-ai/neoruntime` - platform core
- `camthink-ai/neoruntime-sdks` - SDKs used by apps

## License

This repository is licensed under the MIT License. See [LICENSE](./LICENSE).
