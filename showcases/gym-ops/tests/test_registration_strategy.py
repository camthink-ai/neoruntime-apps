"""Offline tests for the incumbent-aware registration strategy
(registration.py) — no SDK import needed (see the module docstring there).

Run: cd showcases/gym-ops && python3 -m pytest tests/ -q
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from registration import (  # noqa: E402
    RegistrationSpec,
    backend_function,
    ensure_model,
    incumbent_variant,
    pose_decoder_active,
)

POSE_REFUSAL = (
    "model id 'yolov8s_pose' is already registered with a different "
    "configuration (type='keypoint' "
    "variant='{\"native_yolov8_pose\": true, \"score_threshold\": 0.35, "
    "\"keypoint_threshold\": 0.25}')"
)
FACIAL_REFUSAL = (
    "model id 'yolov8s_pose' is already registered with a different "
    "configuration (type='keypoint' variant='{}')"
)


class FakeInfer:
    """Duck-typed InferenceClient recording calls, with an optional
    same-id refusal on the first register_model attempt."""

    def __init__(self, refuse_with: str | None = None) -> None:
        self.refuse_with = refuse_with
        self._register_attempts = 0
        self.registrations: list[dict] = []
        self.unregistered: list[str] = []
        self.tunings: list[tuple[str, str]] = []

    def register_model(self, model_path: str, model_id: str,
                       owner_id: str | None = None,
                       model_type: str | None = None,
                       model_variant: str | None = None) -> str:
        self._register_attempts += 1
        if self.refuse_with and self._register_attempts == 1:
            raise RuntimeError(self.refuse_with)
        self.registrations.append({
            "model_path": model_path, "model_id": model_id,
            "owner_id": owner_id, "model_type": model_type,
            "model_variant": model_variant,
        })
        return model_id

    def unregister_model(self, model_id: str) -> None:
        self.unregistered.append(model_id)

    def update_postprocess_config(self, model_id: str, config_json: str) -> bool:
        self.tunings.append((model_id, config_json))
        return True


def pose_spec(**overrides) -> RegistrationSpec:
    base = dict(
        model_path="/data/aipc/models/keypoint/yolov8s_pose.hef",
        model_id="yolov8s_pose",
        owner_id="gym-ops",
        model_type="keypoint",
        variant='{"native_yolov8_pose": true}',
        keep_incumbent=pose_decoder_active,
        tunables_json='{"score_threshold": 0.35}',
    )
    base.update(overrides)
    return RegistrationSpec(**base)


# ---- incumbent_variant parsing ----

def test_incumbent_variant_extracts_pose_blob_from_refusal_text() -> None:
    # Arrange / Act
    parsed = incumbent_variant(POSE_REFUSAL)
    # Assert
    assert parsed is not None
    assert parsed["native_yolov8_pose"] is True
    assert parsed["keypoint_threshold"] == 0.25


def test_incumbent_variant_returns_none_when_refusal_has_no_variant() -> None:
    # Arrange
    msg = "model id 'x' is already registered with a different configuration ()"
    # Act / Assert
    assert incumbent_variant(msg) is None


def test_incumbent_variant_returns_none_on_malformed_json() -> None:
    # Arrange
    msg = "variant='{not json}'"
    # Act / Assert
    assert incumbent_variant(msg) is None


# ---- ensure_model paths ----

def test_registers_directly_when_no_incumbent_exists() -> None:
    # Arrange
    infer = FakeInfer()
    # Act
    mid = ensure_model(infer, pose_spec())
    # Assert
    assert mid == "yolov8s_pose"
    assert len(infer.registrations) == 1
    assert infer.registrations[0]["model_variant"] == '{"native_yolov8_pose": true}'
    assert infer.unregistered == []
    assert infer.tunings == []


def test_keeps_preloaded_pose_registration_and_applies_tunables() -> None:
    # Arrange — preload already carries the decoder flag
    infer = FakeInfer(refuse_with=POSE_REFUSAL)
    # Act
    mid = ensure_model(infer, pose_spec())
    # Assert
    assert mid == "yolov8s_pose"
    assert infer.unregistered == []  # the whole point: no teardown
    assert infer.tunings == [("yolov8s_pose", '{"score_threshold": 0.35}')]
    assert infer.registrations == []  # the refused attempt never landed


def test_replaces_incumbent_missing_the_pose_decoder_flag() -> None:
    # Arrange — facial-default preload (empty variant blob)
    infer = FakeInfer(refuse_with=FACIAL_REFUSAL)
    # Act
    mid = ensure_model(infer, pose_spec())
    # Assert
    assert mid == "yolov8s_pose"
    assert infer.unregistered == ["yolov8s_pose"]
    assert infer.registrations[-1]["model_variant"] == '{"native_yolov8_pose": true}'


def test_reraises_refusals_that_are_not_same_id_conflicts() -> None:
    # Arrange
    infer = FakeInfer(
        refuse_with="Failed to register model 'yolov8s_pose': internal error")
    # Act / Assert
    try:
        ensure_model(infer, pose_spec())
    except RuntimeError as exc:
        assert "internal error" in str(exc)
    else:
        raise AssertionError("expected the registration error to propagate")
    assert infer.unregistered == []


def test_detection_incumbent_is_kept_when_backend_matches() -> None:
    # Arrange — preloaded detection with the hailo_yolov8n backend
    refusal = (
        "model id 'yolov8n' is already registered with a different "
        "configuration (type='detection' "
        "variant='{\"backend_function\": \"hailo_yolov8n\", "
        "\"labels\": [\"person\"]}')"
    )
    infer = FakeInfer(refuse_with=refusal)
    spec = RegistrationSpec(
        model_path="/data/aipc/models/detection/hailo_yolov8n_384_640.hef",
        model_id="yolov8n",
        owner_id="gym-ops",
        model_type="detection",
        variant=None,
        keep_incumbent=backend_function("hailo_yolov8n"),
    )
    # Act
    mid = ensure_model(infer, spec)
    # Assert
    assert mid == "yolov8n"
    assert infer.unregistered == []


def test_detection_incumbent_is_replaced_when_backend_differs() -> None:
    # Arrange — mis-routed incumbent (wrong backend function)
    refusal = (
        "model id 'yolov8n' is already registered with a different "
        "configuration (type='detection' "
        "variant='{\"backend_function\": \"yolov5m_vehicles\"}')"
    )
    infer = FakeInfer(refuse_with=refusal)
    spec = RegistrationSpec(
        model_path="/data/aipc/models/detection/hailo_yolov8n_384_640.hef",
        model_id="yolov8n",
        owner_id="gym-ops",
        model_type="detection",
        variant=None,
        keep_incumbent=backend_function("hailo_yolov8n"),
    )
    # Act
    ensure_model(infer, spec)
    # Assert
    assert infer.unregistered == ["yolov8n"]
    assert json.loads(infer.registrations[-1]["model_variant"] or "null") is None
