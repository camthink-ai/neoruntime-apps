"""GymOps configuration loader.

Loads from environment (primary, set by app.yaml) with optional YAML overlay at
CONFIG_PATH. Mirrors the parking-lot config.py pattern: env-driven + MODEL_DEFS
dict + infer timeout tiering constants.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

import yaml


# ---- Infer timeout tiering (mirrors parking-lot, tuned for keypoint model) ----
INFER_TIMEOUT_MS = 1500          # steady-state per-infer timeout
INFER_WARMUP_TIMEOUT_MS = 6000   # first N inferences after model load
INFER_WARMUP_COUNT = 3
DEGRADED_FAILURE_THRESHOLD = 10  # consecutive failures before degraded mode
# Bounded startup retry: consecutive pose-registration failures before the
# infer loop gives up and reports unhealthy (the container healthcheck then
# fails and restart_policy=on-failure restarts us). 12 × 5s ≈ 1 minute.
MODEL_REGISTER_MAX_FAILURES = 12


# ---- COCO-17 keypoint indices (must match hal_internal_yolov8_pose kNumKpts=17) ----
NOSE = 0
L_EYE, R_EYE = 1, 2
L_EAR, R_EAR = 3, 4
L_SHOULDER, R_SHOULDER = 5, 6
L_ELBOW, R_ELBOW = 7, 8
L_WRIST, R_WRIST = 9, 10
L_HIP, R_HIP = 11, 12
L_KNEE, R_KNEE = 13, 14
L_ANKLE, R_ANKLE = 15, 16

# 16 skeleton edges, mirroring hal_internal_yolov8_pose kJointPairs
SKELETON_EDGES = [
    (NOSE, L_EYE), (L_EYE, L_EAR), (NOSE, R_EYE), (R_EYE, R_EAR),
    (L_SHOULDER, R_SHOULDER),
    (L_SHOULDER, L_ELBOW), (L_ELBOW, L_WRIST),
    (R_SHOULDER, R_ELBOW), (R_ELBOW, R_WRIST),
    (L_HIP, R_HIP),
    (L_SHOULDER, L_HIP), (R_SHOULDER, R_HIP),
    (L_HIP, L_KNEE), (L_KNEE, L_ANKLE),
    (R_HIP, R_KNEE), (R_KNEE, R_ANKLE),
]


@dataclass(frozen=True)
class ModelDef:
    name: str
    path: str
    model_type: str          # "keypoint" for pose
    variant: str             # "yolov8s_pose" etc.
    input_format: str        # "nv12"
    input_size: tuple[int, int] = (640, 640)  # RGB WxH for single-frame infer()
    config_json: dict = field(default_factory=dict)  # extra postprocess config
    # Full postprocess JSON blob supplied AT REGISTRATION time as the model
    # variant. For keypoint pose this is the ONLY channel that flips
    # native_yolov8_pose at HAL create() (see app.py Stage 0); the runtime
    # update_postprocess_config path does NOT handle native_yolov8_pose.
    postprocess_json: str = ""


# Registered models. yolov8s_pose is the PRD default for keypoint;
# yolov8n is the default secondary detection model for parallel inference.
# NOTE: HEF must be provisioned on device (see Stage 0 gate in plan).
MODEL_DEFS: dict[str, ModelDef] = {
    "yolov8s_pose": ModelDef(
        name="yolov8s_pose",
        # keypoint/ prefix matches the device model-store layout (global
        # store: /data/aipc/models/keypoint/yolov8s_pose.hef) — the same
        # <category>/<file> convention as the yolov8n entry below and as
        # model-showcase. Only used when the app self-registers (platforms
        # without spec.models preload); the primary path is the AMPK bundle
        # declared in app.yaml spec.models.
        path="keypoint/yolov8s_pose.hef",
        model_type="keypoint",
        variant="yolov8s_pose",
        input_format="nv12",
        # native_yolov8_pose forces HAL's built-in YOLOv8-Pose decoder instead
        # of the facial_landmarks_nv12 default plugin. This flag is read by HAL
        # create() from merged_vendor_json (hailo15_postprocess_impl.cpp:1127),
        # so it MUST be supplied at registration time via postprocess_json
        # (carried as the model variant — the only app→init_post_process
        # channel). The runtime update_postprocess_config path does NOT handle
        # native_yolov8_pose. Keys mirror
        # hal_v2/examples/ai_example_v2/data/yolov8_pose_native_post.example.json.
        postprocess_json=json.dumps({
            "native_yolov8_pose": True,
            "yolov8_pose_network_width": 640,
            "yolov8_pose_network_height": 640,
            "iou_threshold": 0.7,
            # Person-box score gate. This create-time blob is the EFFECTIVE
            # filter for native_yolov8_pose (runtime update_postprocess_config
            # ignores it, see comment above). 0.6 dropped distant/small
            # gym-goers on the 640x640 input; 0.35 recovers them.
            "score_threshold": 0.35,
            "keypoint_threshold": 0.25,
            "confidence_threshold": 0.35,
        }),
        config_json={"native_yolov8_pose": True},
    ),
    "yolov8n": ModelDef(
        name="yolov8n",
        path="detection/hailo_yolov8n_384_640.hef",
        model_type="detection",
        variant="yolov8n",
        input_format="nv12",
        input_size=(640, 384),  # WxH from filename: 640 wide × 384 tall
    ),
    "mobilefacenet": ModelDef(
        name="mobilefacenet",
        # DORMANT (0.1.2): face recognition was never wired past config —
        # no registration, no inference call, no HEF bundled. Kept to
        # document the planned 112x112 embedding model; FACE_MODEL stays
        # "" in app.yaml until the pipeline is actually implemented.
        path="face/mobilefacenet.hef",
        model_type="embedding",
        variant="mobilefacenet",
        input_format="rgb",
        input_size=(112, 112),
        config_json={"normalize": True},
    ),
}


@dataclass
class GymConfig:
    # --- web ---
    web_port: int = 8890
    log_level: str = "INFO"

    # --- inference ---
    stream_id: str = "sub"            # preview stream
    infer_stream_id: str = "sub"      # inference stream (may differ)
    infer_fps: int = 10
    preview_fps: int = 30          # MJPEG preview render fps (decoupled from infer_fps)
    pose_model: str = "yolov8s_pose"
    pose_confidence_threshold: float = 0.35
    keypoint_threshold: float = 0.25
    score_threshold: float = 0.35
    nms_threshold: float = 0.7
    model_root: str = "/data/aipc/models"

    # --- parallel inference (secondary detection model) ---
    detect_model: str = "yolov8n"     # secondary detection model; "" = disabled
    enable_parallel_infer: bool = True  # use infer_batch() for dual-model
    detect_every: int = 1   # run detection every Nth frame (1=every frame); pose runs every frame
    # Depth-N async inference pipeline: how many frames to keep in-flight on the
    # NPU at once. 1 = serial (legacy behavior, the kill-switch). >1 overlaps
    # frame N+1's NPU job with frame N's await+postproc, raising pose throughput
    # without raising per-frame latency. See _run_live_inference.
    pipeline_depth: int = 2
    stats_interval_seconds: float = 1.0  # min seconds between get_stats() RPCs (the RPC is ~0.5s; util is sampled at most this often)

    # --- platform-api (H264 MSE preview) ---
    platform_api_ws_scheme: str = "wss"
    platform_api_port: int = 443
    platform_api_token: str = ""

    # --- alerts / occupancy timing ---
    alert_cooldown_seconds: int = 10
    equipment_occupancy_seconds: int = 5
    long_occupation_seconds: int = 1800     # 30 min on one equipment
    fall_candidate_seconds: int = 6
    long_static_seconds: int = 120

    # --- zones (loaded from YAML overlay, immutable runtime default) ---
    zones: list[dict] = field(default_factory=list)
    equipment: list[dict] = field(default_factory=list)
    after_hours: dict = field(default_factory=dict)  # {"start":"22:00","end":"06:00"}

    # --- face recognition (Phase 2) ---
    # "" since 0.1.2: the mobilefacenet pipeline is dormant (see MODEL_DEFS)
    # and the app.yaml declaration was disabled to keep the manifest honest.
    face_model: str = ""                      # embedding model; "" = disabled
    face_db_path: str = "/data/aipc/etc/gym-ops/faces.json"
    face_threshold: float = 0.6               # cosine similarity threshold
    face_cooldown_seconds: float = 30.0       # min seconds between re-ID for same person

    # --- audio / access control (Phase 3) ---
    audio_dir: str = "/data/aipc/etc/gym-ops/audio"
    audio_cooldown_seconds: float = 5.0
    enable_gate: bool = False                 # Wiegand gate unlock on member ID
    enable_alarm: bool = True                 # Alarm output on safety events
    gate_unlock_seconds: float = 5.0          # auto-relock after this many seconds

    # --- trajectory (zone-visit sessions) ---
    enable_trajectory: bool = True            # master switch for the feature
    trajectory_min_visit_seconds: float = 3.0     # debounce: dwell needed to count as a visit
    trajectory_rejoin_seconds: float = 30.0       # quick return to same zone merges into one visit
    trajectory_session_close_seconds: float = 10.0  # person unseen this long closes their session
    trajectory_trail_points: int = 60         # overlay trail ring-buffer length per person
    trajectory_persist: bool = True           # write session summaries to JSONL
    trajectory_link_member_identity: bool = False  # privacy: keep member_id on disk (default strip)
    trajectory_retention_days: int = 30       # JSONL files older than this are pruned
    trajectory_dir: str = "/data/aipc/etc/gym-ops/trajectory"
    trajectory_rebind_counter: bool = True    # rebuild counter on visit_confirmed when exercise differs

    # --- config source ---
    config_path: str = ""

    def model_def(self) -> ModelDef:
        return MODEL_DEFS[self.pose_model]

    # Model-path resolution contract (fixed in 0.1.2): registration paths
    # must exist on the HOST filesystem, because ai-runtime runs host-side
    # and opens the path itself. Image-internal paths (e.g. /opt/aipc/*)
    # are translated by the SDK's translate_path_to_host() to a /data/aipc
    # counterpart that only exists for platform-installed content — never
    # for files COPY'd into the image, so the old bundled-model fallback
    # registered paths ai-runtime could not open (HAILO_OPEN_FILE_FAILURE).
    # The primary model source is therefore the AMPK bundle declared in
    # app.yaml spec.models (app-manager unpacks it host-side and preloads
    # the registration); this resolver only serves the self-registration
    # fallback against the device model store.
    _BUNDLED_MODEL_ROOT = "/opt/aipc/bundled-models"  # legacy, unused at runtime

    def _resolve_model_path(self, rel: str) -> str:
        """Resolve a model file to a HOST-visible path, or fail loudly.

        Host model store first (category-prefixed layout, same convention
        as model-showcase). A missing store copy is an error, never a
        fallback to an image-internal path — the bundled fallback used to
        hand ai-runtime a host path that did not exist and the app retried
        forever (see the contract comment above).
        """
        host = os.path.join(self.model_root, rel)
        if os.path.isfile(host):
            return host
        raise RuntimeError(
            f"model file not found on the host-visible model store: {host} "
            f"(rel={rel!r}). Expected spec.models preload to register this "
            f"model; on platforms without bundled-package support, copy the "
            f"HEF into the device model store at that path."
        )

    def model_full_path(self) -> str:
        return self._resolve_model_path(self.model_def().path)

    def detect_model_def(self) -> ModelDef | None:
        """Return ModelDef for the secondary detection model, or None if disabled."""
        if not self.detect_model or self.detect_model not in MODEL_DEFS:
            return None
        return MODEL_DEFS[self.detect_model]

    def detect_model_full_path(self) -> str | None:
        """Full path to secondary detection HEF, or None if disabled."""
        ddef = self.detect_model_def()
        if ddef is None:
            return None
        return self._resolve_model_path(ddef.path)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, str(default)))
    except ValueError:
        return default


def _load_yaml_overlay(path: str) -> dict[str, Any]:
    """Load optional YAML overlay. Returns {} if missing/unreadable."""
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, yaml.YAMLError) as e:
        print(f"[config] WARN: failed to load YAML overlay {path}: {e}")
        return {}


def load_config() -> GymConfig:
    """Build GymConfig from env, then apply YAML overlay for zones/equipment."""
    config_path = _env("CONFIG_PATH", "/data/aipc/etc/gym-ops/config.yaml")

    cfg = GymConfig(
        web_port=_env_int("WEB_PORT", 8890),
        log_level=_env("LOG_LEVEL", "INFO"),
        stream_id=_env("STREAM_ID", "sub"),
        infer_stream_id=_env("INFER_STREAM_ID", "sub"),
        infer_fps=_env_int("INFER_FPS", 10),
        preview_fps=_env_int("PREVIEW_FPS", 30),
        pose_model=_env("POSE_MODEL", "yolov8s_pose"),
        pose_confidence_threshold=_env_float("POSE_CONFIDENCE_THRESHOLD", 0.35),
        keypoint_threshold=_env_float("KEYPOINT_THRESHOLD", 0.25),
        score_threshold=_env_float("SCORE_THRESHOLD", 0.35),
        nms_threshold=_env_float("NMS_THRESHOLD", 0.7),
        model_root=_env("MODEL_ROOT", "/data/aipc/models"),
        detect_model=_env("DETECT_MODEL", "yolov8n"),
        enable_parallel_infer=_env("ENABLE_PARALLEL_INFER", "true").lower() in ("true", "1", "yes"),
        detect_every=_env_int("DETECT_EVERY", 1),
        pipeline_depth=_env_int("PIPELINE_DEPTH", 2),
        stats_interval_seconds=_env_float("STATS_INTERVAL_SECONDS", 1.0),
        platform_api_ws_scheme=_env("PLATFORM_API_WS_SCHEME", "wss"),
        platform_api_port=_env_int("PLATFORM_API_PORT", 443),
        platform_api_token=_env("PLATFORM_API_TOKEN", ""),
        alert_cooldown_seconds=_env_int("ALERT_COOLDOWN_SECONDS", 10),
        equipment_occupancy_seconds=_env_int("EQUIPMENT_OCCUPANCY_SECONDS", 5),
        long_occupation_seconds=_env_int("LONG_OCCUPATION_SECONDS", 1800),
        fall_candidate_seconds=_env_int("FALL_CANDIDATE_SECONDS", 6),
        long_static_seconds=_env_int("LONG_STATIC_SECONDS", 120),
        config_path=config_path,
        face_model=_env("FACE_MODEL", ""),
        face_db_path=_env("FACE_DB_PATH", "/data/aipc/etc/gym-ops/faces.json"),
        face_threshold=_env_float("FACE_THRESHOLD", 0.6),
        face_cooldown_seconds=_env_float("FACE_COOLDOWN_SECONDS", 30.0),
        audio_dir=_env("AUDIO_DIR", "/data/aipc/etc/gym-ops/audio"),
        audio_cooldown_seconds=_env_float("AUDIO_COOLDOWN_SECONDS", 5.0),
        enable_gate=_env("ENABLE_GATE", "false").lower() in ("true", "1", "yes"),
        enable_alarm=_env("ENABLE_ALARM", "true").lower() in ("true", "1", "yes"),
        gate_unlock_seconds=_env_float("GATE_UNLOCK_SECONDS", 5.0),
        enable_trajectory=_env("ENABLE_TRAJECTORY", "true").lower() in ("true", "1", "yes"),
        trajectory_min_visit_seconds=_env_float("TRAJECTORY_MIN_VISIT_SECONDS", 3.0),
        trajectory_rejoin_seconds=_env_float("TRAJECTORY_REJOIN_SECONDS", 30.0),
        trajectory_session_close_seconds=_env_float("TRAJECTORY_SESSION_CLOSE_SECONDS", 10.0),
        trajectory_trail_points=_env_int("TRAJECTORY_TRAIL_POINTS", 60),
        trajectory_persist=_env("TRAJECTORY_PERSIST", "true").lower() in ("true", "1", "yes"),
        trajectory_link_member_identity=_env("TRAJECTORY_LINK_MEMBER_IDENTITY", "false").lower() in ("true", "1", "yes"),
        trajectory_retention_days=_env_int("TRAJECTORY_RETENTION_DAYS", 30),
        trajectory_dir=_env("TRAJECTORY_DIR", "/data/aipc/etc/gym-ops/trajectory"),
        trajectory_rebind_counter=_env("TRAJECTORY_REBIND_COUNTER", "true").lower() in ("true", "1", "yes"),
    )

    overlay = _load_yaml_overlay(config_path)
    if overlay:
        cfg.zones = overlay.get("zones", []) or []
        cfg.equipment = overlay.get("equipment", []) or []
        cfg.after_hours = overlay.get("after_hours", {}) or {}

    # validate model selection
    if cfg.pose_model not in MODEL_DEFS:
        raise ValueError(
            f"Unknown pose_model '{cfg.pose_model}'. "
            f"Available: {list(MODEL_DEFS.keys())}"
        )
    if cfg.detect_model and cfg.detect_model not in MODEL_DEFS:
        raise ValueError(
            f"Unknown detect_model '{cfg.detect_model}'. "
            f"Available: {list(MODEL_DEFS.keys())} (empty string to disable)"
        )

    return cfg
