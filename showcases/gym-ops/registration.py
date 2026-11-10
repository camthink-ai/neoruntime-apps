"""Incumbent-aware model registration (parking-lot pattern, see
parking_lot/app.py ``_register_one``).

app-manager's PreloadModels (fed by the spec.models AMPK bundles) registers
manifest-declared models before the app starts, with a platform-composed
variant. ai-runtime refuses a same-id re-registration whose configuration
differs, embedding the incumbent variant in the refusal text:

    "model id 'X' is already registered with a different configuration
     (type='keypoint' variant='{"native_yolov8_pose": true, ...}')"

Strategy: register our own variant; on an "already registered" refusal,
KEEP the incumbent when it already routes through the expected decoder or
backend (advisory differences — thresholds, label tables — are fine, the
numeric knobs stay hot-tunable via update_postprocess_config), otherwise
force-replace it (empty-owner unregister is ai-runtime's system-level
unload) and re-apply our variant.

SDK-free on purpose: the acceptance predicates and the refusal parser are
pure functions so tests can exercise them without neoruntime-ipc-sdk
installed (app.py imports the SDK at module level and is therefore not
importable off-device).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Protocol

# Refusal shape (ai-runtime): variant='{"…"}' — non-greedy so a trailing
# quote inside the JSON object still matches up to the closing brace.
INCUMBENT_VARIANT_RE = re.compile(r"variant='(\{.*?\})'")


def incumbent_variant(err_msg: str) -> dict | None:
    """Extract the incumbent variant blob from a registration refusal.

    Parse failures read as None so the caller falls through to the
    replace path (a blob we cannot understand is not one we vouched for).
    """
    match = INCUMBENT_VARIANT_RE.search(err_msg)
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


# ---- incumbent acceptance predicates ----

def pose_decoder_active(incumbent: dict) -> bool:
    """The preloaded keypoint registration already selected HAL's built-in
    YOLOv8-Pose decoder (create-time flag — the one thing we must not lose)."""
    return incumbent.get("native_yolov8_pose") is True


def backend_function(expected: str) -> Callable[[dict], bool]:
    """Build a predicate asserting the incumbent routes through a given
    postprocess backend_function (detection models)."""
    def check(incumbent: dict) -> bool:
        return incumbent.get("backend_function") == expected
    return check


class InferenceClientProtocol(Protocol):
    """Duck-typed slice of neoruntime_ipc_sdk.InferenceClient used here."""

    def register_model(self, model_path: str, model_id: str,
                       owner_id: str | None = None,
                       model_type: str | None = None,
                       model_variant: str | None = None) -> str: ...

    def unregister_model(self, model_id: str) -> None: ...

    def update_postprocess_config(self, model_id: str, config_json: str) -> bool: ...


@dataclass(frozen=True)
class RegistrationSpec:
    """Everything ensure_model needs to register (or accept) one model."""
    model_path: str
    model_id: str
    owner_id: str
    model_type: str
    variant: str | None = None
    keep_incumbent: Callable[[dict], bool] | None = None
    # Hot-tunable postprocess knobs applied after accepting an incumbent
    # (create-time flags must NOT go here — they live in the variant).
    tunables_json: str | None = None
    log: Callable[[str], None] = field(default=lambda _msg: None)


def ensure_model(infer: InferenceClientProtocol, spec: RegistrationSpec) -> str:
    """Register ``spec.model_id`` or accept an already-correct incumbent.

    Raises whatever the final registration attempt raises — callers gate
    their startup loop on that (same contract as the old direct call).
    """
    try:
        infer.register_model(
            model_path=spec.model_path,
            model_id=spec.model_id,
            owner_id=spec.owner_id,
            model_type=spec.model_type,
            model_variant=spec.variant,
        )
        spec.log(f"[app] model registered id={spec.model_id}")
        return spec.model_id
    except Exception as exc:  # noqa: BLE001 — refusal text is the payload
        msg = str(exc)
        if "already registered" not in msg:
            raise
        incumbent = incumbent_variant(msg)
        if (incumbent is not None and spec.keep_incumbent is not None
                and spec.keep_incumbent(incumbent)):
            spec.log(f"[app] keeping preloaded registration for {spec.model_id} "
                     f"(incumbent variant already active)")
            if spec.tunables_json:
                try:
                    infer.update_postprocess_config(spec.model_id, spec.tunables_json)
                except Exception as tun_exc:  # noqa: BLE001 — best-effort tuning
                    spec.log(f"[app] update_postprocess_config failed (non-fatal): {tun_exc}")
            return spec.model_id
        spec.log(f"[app] replacing mis-configured registration for {spec.model_id}: {msg}")
        infer.unregister_model(model_id=spec.model_id)
        infer.register_model(
            model_path=spec.model_path,
            model_id=spec.model_id,
            owner_id=spec.owner_id,
            model_type=spec.model_type,
            model_variant=spec.variant,
        )
        spec.log(f"[app] model re-registered id={spec.model_id}")
        return spec.model_id
