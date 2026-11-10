#!/usr/bin/env python3
"""Offline AMPK package builder for app-bundled models (spec.models).

Emits the single-file .bin distribution packages that app manifests declare
under spec.models.<alias>.path. The format is defined by the platform's
storage package reader (platform/platform-api/storage/modelpackage.go):

    0   4   magic "AMPK"
    4   2   version (2)
    6   2   flags (reserved, 0)
    8   4   json_len  (big-endian)
    12  8   hef_len   (big-endian)
    20  32  sha256(json_bytes || hef_bytes)
    52  ..  json_bytes, then hef_bytes (byte-identical to the source HEF)

The JSON metadata schema is CLOSED — only the keys the platform reader knows
are emitted. At install time app-manager unpacks the package, stages the HEF
under {RootPath}/app-models/<app>/<alias>/ and composes the runtime
registration from the metadata (model_type + postprocess_profile config key
select the postprocess backend; network dimensions feed the keypoint pose
blob's create-only size keys).

This script exists so packages can be regenerated from the vendored .hef
artifacts without a live device: the embedded HEF bytes stay identical to
what the app registers at runtime.

Usage:
    scripts/make_ampk_package.py --hef models/yolov8s_pose.hef \
        --out models/yolov8s_pose.bin \
        --model-id yolov8s_pose --model-type keypoint \
        --profile yolov8_pose \
        --config threshold=0.35 --config keypoint_threshold=0.25 \
        --net-width 640 --net-height 640
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path

MAGIC = b"AMPK"
PACKAGE_VERSION = 2
HEADER = struct.Struct(">4sHHIQ32s")

# postprocess_profile values the platform validates against its registry.
KNOWN_PROFILES = {
    "keypoint": {"facial_landmarks", "yolov8_pose"},
    "detection": {
        "hailo_yolov8n", "hailo_yolov8n_384_640", "tiny_yolov4_lp",
        "yolov5m_vehicles", "native_yolov8_pose",
    },
}


def parse_config_kv(pairs: list[str]) -> dict:
    """Turn repeated --config key=json_value args into a JSON object."""
    config: dict = {}
    for pair in pairs:
        if "=" not in pair:
            raise SystemExit(f"--config expects key=json_value, got: {pair!r}")
        key, _, raw = pair.partition("=")
        try:
            config[key.strip()] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--config {key!r}: value is not valid JSON ({raw!r}): {exc}") from exc
    return config


def build_meta(args: argparse.Namespace, hef_sha256: str) -> dict:
    meta: dict = {
        "format_version": PACKAGE_VERSION,
        "model_id": args.model_id,
        "model_type": args.model_type,
        "config": {"postprocess_profile": args.profile, **parse_config_kv(args.config)},
        "hef": {"filename": Path(args.hef).name, "sha256": hef_sha256},
        "network": {
            "name": args.net_name or Path(args.hef).stem,
            "input_width": args.net_width,
            "input_height": args.net_height,
        },
    }
    if args.name:
        meta["name"] = args.name
    if args.output_mode:
        meta["output_mode"] = args.output_mode
    if args.output_format:
        meta["output_format"] = args.output_format
    return meta


def write_package(out_path: Path, meta: dict, hef_bytes: bytes) -> None:
    json_bytes = json.dumps(meta, indent=2).encode("utf-8")
    if len(json_bytes) > (1 << 20):
        raise SystemExit(f"package metadata too large ({len(json_bytes)} bytes)")
    digest = hashlib.sha256(json_bytes + hef_bytes).digest()
    header = HEADER.pack(
        MAGIC, PACKAGE_VERSION, 0, len(json_bytes), len(hef_bytes), digest
    )
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_bytes(header + json_bytes + hef_bytes)
    tmp.replace(out_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--hef", required=True, help="source HEF (vendored device artifact)")
    parser.add_argument("--out", required=True, help="output .bin package path")
    parser.add_argument("--model-id", required=True, help="registration model id")
    parser.add_argument("--name", help="display name (defaults to omitted)")
    parser.add_argument("--model-type", required=True,
                        choices=["keypoint", "detection", "segmentation", "classification",
                                 "ocr_recognition", "depth", "clip", "genai", "embedding"])
    parser.add_argument("--profile", required=True,
                        help="postprocess_profile config value (platform registry key)")
    parser.add_argument("--config", action="append", default=[],
                        metavar="KEY=JSON", help="extra config keys, repeatable")
    parser.add_argument("--net-name", help="network name (defaults to HEF stem)")
    parser.add_argument("--net-width", type=int, default=0)
    parser.add_argument("--net-height", type=int, default=0)
    parser.add_argument("--output-mode", default="platform", choices=["platform", "raw"])
    parser.add_argument("--output-format", choices=["nms", "feature_map"])
    args = parser.parse_args(argv)

    known = KNOWN_PROFILES.get(args.model_type)
    if known is not None and args.profile not in known:
        print(f"warning: profile {args.profile!r} is not in the known set for "
              f"{args.model_type}: {sorted(known)}", file=sys.stderr)

    hef_path = Path(args.hef)
    if not hef_path.is_file():
        raise SystemExit(f"HEF not found: {hef_path}")
    hef_bytes = hef_path.read_bytes()
    hef_sha = hashlib.sha256(hef_bytes).hexdigest()

    meta = build_meta(args, hef_sha)
    out_path = Path(args.out)
    write_package(out_path, meta, hef_bytes)
    print(f"wrote {out_path} ({out_path.stat().st_size} bytes) "
          f"model_id={meta['model_id']} type={meta['model_type']} "
          f"profile={args.profile} hef_sha256={hef_sha[:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
