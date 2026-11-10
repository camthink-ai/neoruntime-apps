"""GymOps Pose Coach — application entry point.

Flask web server + background inference loop. The loop subscribes to the
platform inference stream (yolov8s_pose keypoint model), parses COCO-17
landmarks per detected person, runs rep counters / zone occupancy / safety
alerts, serves an MJPEG preview, and pushes normalized snapshots to SSE
clients. Also supports an uploaded-test-video mode.

Stage 0 device gate (see plan): after register_model(keypoint) we call
update_postprocess_config with native_yolov8_pose=True so HAL uses its
built-in YOLOv8-Pose decoder instead of the face-landmarks default plugin.
"""
from __future__ import annotations

import json
import os
import queue
import signal
import threading
import time
import urllib.parse
from collections import deque

import cv2
import numpy as np
from flask import Flask, Response, jsonify, render_template, request
from flask_sock import Sock

from neoruntime_ipc_sdk.app import AppClient
from neoruntime_ipc_sdk.config import Config
from neoruntime_ipc_sdk.events import EventClient
from neoruntime_ipc_sdk.inference import BatchInferItem, InferenceClient
from neoruntime_ipc_sdk.media import FdMediaClient

import overlay
from alerts import (
    AlertManager, TOPIC_OCCUPANCY, TOPIC_POSE, TOPIC_IDENTITY,
    TOPIC_TRAJECTORY,
)
from config import (
    DEGRADED_FAILURE_THRESHOLD, INFER_TIMEOUT_MS, INFER_WARMUP_COUNT,
    INFER_WARMUP_TIMEOUT_MS, MODEL_REGISTER_MAX_FAILURES, load_config,
)
from registration import (
    RegistrationSpec, backend_function, ensure_model, pose_decoder_active,
)
from counter import make_counter
from exercise_classifier import ExerciseClassifier
from equipment_stats import EquipmentStats
from face_db import FaceDatabase
from audio_prompt import AudioPrompt
from access_control import AccessControl
from pose import KeyPoints, Pt, body_center, parse_keypoints, point_in_polygon
from tracker import BodyTracker
from video_source import VideoFrameSource
from zones import ZoneManager
from trajectory import TrajectoryStore, TrajectoryTracker

# how long an idle person's counter persists before eviction
_TRACKER_TTL_SECONDS = 30
# period for periodic gym/pose + gym/occupancy snapshots on the event bus
_EVENT_SNAPSHOT_SECONDS = 2.0
_EXERCISE = os.environ.get("EXERCISE", "squat")  # squat | pushup

# --- overlay temporal interpolation (preview-fps decoupled from infer-fps) ---
# Between two inference frames the preview thread linearly extrapolates
# keypoints by velocity so the skeleton tracks the person instead of lagging
# by 1/infer_fps. Clamps prevent overshoot on sudden stops / turns.
_MAX_PREDICT_HORIZON = 0.20   # don't extrapolate past 200ms (fall back to last)
_MAX_EXTRAP_DISPL = 0.08      # max per-keypoint displacement, normalized [0,1]
_MIN_INFER_DT = 0.01          # min infer interval for velocity (div-by-zero guard)


class GymApp:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.app = Flask(__name__, template_folder="templates",
                         static_folder="static")
        self.sock = Sock(self.app)

        self.infer = InferenceClient()
        self.events = EventClient()
        self.media = FdMediaClient()
        # Dedicated infer-frame source. The preview thread already owns
        # self.media on the preview stream; live single-shot infer() needs its
        # own FdMediaClient so the two threads don't race one UDS socket
        # (mirrors model-showcase's media / media_infer split).
        self.media_infer = FdMediaClient()

        self.zones = ZoneManager(
            self.cfg.zones, self.cfg.equipment,
            self.cfg.equipment_occupancy_seconds,
            self.cfg.long_occupation_seconds,
        )
        # zones flagged forbidden in config (zone dict: forbidden: true)
        self.forbidden_zones = {z.id for z in self.zones.zones if z.forbidden}
        self.alerts = AlertManager(
            self.events, self.cfg.alert_cooldown_seconds,
            self.cfg.fall_candidate_seconds, self.cfg.long_static_seconds,
        )

        # Phase 1: exercise classifier (zone→exercise mapping from equipment)
        self.exercise_classifier = ExerciseClassifier(
            self.zones.zone_exercise_map)

        # Phase 1+4: equipment utilization tracking
        self.equipment_stats = EquipmentStats()

        # Trajectory: zone-visit sessions (kill-switch via ENABLE_TRAJECTORY)
        self.trajectory: TrajectoryTracker | None = None
        self.trajectory_store: TrajectoryStore | None = None
        if self.cfg.enable_trajectory:
            if self.cfg.trajectory_persist:
                self.trajectory_store = TrajectoryStore(
                    self.cfg.trajectory_dir,
                    retention_days=self.cfg.trajectory_retention_days,
                    link_member_identity=self.cfg.trajectory_link_member_identity,
                )
                self.trajectory_store.prune()
            self.trajectory = TrajectoryTracker(
                zone_names={z.id: z.name for z in self.zones.zones},
                zone_exercise_map=self.zones.zone_exercise_map,
                min_visit_seconds=self.cfg.trajectory_min_visit_seconds,
                rejoin_seconds=self.cfg.trajectory_rejoin_seconds,
                session_close_seconds=self.cfg.trajectory_session_close_seconds,
                trail_points=self.cfg.trajectory_trail_points,
                store=self.trajectory_store,
            )

        # Phase 2: face recognition (optional — disabled if model missing)
        self.face_db: FaceDatabase | None = None
        self._face_model_id: str | None = None
        if self.cfg.face_model:
            try:
                self.face_db = FaceDatabase(
                    self.cfg.face_db_path, self.cfg.face_threshold)
            except Exception as e:  # noqa: BLE001
                print(f"[app] face DB init failed (face recognition disabled): {e}")
                self.face_db = None
        # member_id mapping: tracker_id → member_id (updated each infer)
        self._member_ids: dict[str, str] = {}
        self._member_ids_ts: dict[str, float] = {}  # tracker_id → last ID ts

        # Phase 3: audio prompts + access control
        self.audio = AudioPrompt(
            self.cfg.audio_dir,
            cooldown_seconds=self.cfg.audio_cooldown_seconds)
        self.access = AccessControl(
            unlock_duration=self.cfg.gate_unlock_seconds)

        # per-tracker rep counters (key = stable "p{n}" from BodyTracker)
        self._counters: dict[str, object] = {}
        self._counter_last_seen: dict[str, float] = {}
        self._tracker = BodyTracker()
        # last periodic gym/pose + gym/occupancy publish (throttle)
        self._last_event_ts = 0.0

        # latest snapshot for /api/state
        self._state_lock = threading.Lock()
        self._latest: dict = {"ts": 0.0, "persons": [], "zones": {}, "fps": None}
        # SSE fan-out
        self._sse_clients: list[queue.Queue] = []
        self._sse_lock = threading.Lock()

        # MJPEG latest JPEG
        self._jpeg_lock = threading.Lock()
        self._latest_jpeg: bytes | None = None
        self._stream_clients = 0
        self._stream_clients_lock = threading.Lock()

        # Synchronized pose metadata for H.264 preview overlay. Video stays on
        # the platform hardware encoder; this channel carries only lightweight
        # inference snapshots.
        self._sync_cond = threading.Condition()
        self._sync_seq = 0
        self._latest_sync_packet: tuple[int, str] | None = None

        # latest overlay context. Live MJPEG sends bare frames and lets the
        # browser draw snapshots on Canvas; uploaded videos still bake overlay
        # server-side from their inference frame.
        self._overlay_lock = threading.Lock()
        self._last_overlay: tuple | None = None  # (persons, snapshots, zone_snap, fps, ts)
        # previous overlay for temporal interpolation (velocity source); None until 2nd frame
        self._prev_overlay: tuple | None = None

        # infer loop control
        self._running = threading.Event()
        self._running.set()
        self._video_source: VideoFrameSource | None = None
        self._video_mode = False
        self._consec_fail = 0
        self._warmup_left = INFER_WARMUP_COUNT
        self._infer_thread: threading.Thread | None = None
        self._stats_thread: threading.Thread | None = None  # bg device_utilization sampler
        self._model_id: str | None = None
        self._detect_model_id: str | None = None  # secondary detection model
        # Bounded startup retry + fatal health reporting (0.1.2): the old
        # loop retried registration forever while the web shell stayed
        # "healthy", hiding a dead inference core from the platform.
        self._register_failures = 0
        self._fatal_error: str | None = None
        # P0: detect decimation + measured pose QPS
        self._infer_frame_seq = 0
        self._last_detect_result = None  # reused on decimated frames for zone/count stability
        self._infer_ts: deque = deque(maxlen=128)  # trailing infer timestamps -> measured pose fps
        # P0(b): per-segment profiling. get_stats() cost is measured inside
        # _on_infer (single call per frame) and read/reset by the live loop.
        self._prof_get_stats_ms: float = 0.0
        # P0(c): get_stats() is a ~0.5s RPC and dominated the live loop
        # (~85% of each frame). Throttle it to cfg.stats_interval_seconds and
        # reuse the last device_utilization between samples.
        self._stats_last_du: float | None = None
        self._stats_last_ts: float = 0.0

        self._register_routes()

    # ---- model registration (Stage 0 gate) ----
    def _register_model(self) -> str:
        mdef = self.cfg.model_def()
        # For keypoint pose the native_yolov8_pose flag (which selects HAL's
        # built-in COCO-17 decoder over the facial_landmarks_nv12 default) is
        # read by HAL create() from merged_vendor_json at REGISTRATION time.
        # The only app→init_post_process channel is model_variant, so the full
        # postprocess_json blob is carried as the variant. The runtime
        # update_postprocess_config path does NOT handle native_yolov8_pose and
        # does not re-run backend selection, so it cannot flip the flag.
        #
        # Since 0.1.2 the platform preloads this model from the spec.models
        # AMPK bundle (its config pins postprocess_profile=yolov8_pose, so
        # the preloaded variant already carries the decoder flag). When the
        # preload is present and correct we KEEP it — re-registering over it
        # used to corrupt the postprocess session. Only a mis-configured
        # incumbent (e.g. a facial-default row) is force-replaced. See
        # registration.ensure_model.
        variant = mdef.postprocess_json or mdef.variant
        # Runtime threshold tuning only (apply_config_json handles these
        # numeric keys). native_yolov8_pose is intentionally absent here — it
        # is already baked into the create-time blob above (ours or the
        # preload's).
        tunables = json.dumps({
            "score_threshold": self.cfg.score_threshold,
            "keypoint_threshold": self.cfg.keypoint_threshold,
            "nms_threshold": self.cfg.nms_threshold,
            "confidence_threshold": self.cfg.pose_confidence_threshold,
        })
        return ensure_model(self.infer, RegistrationSpec(
            model_path=self.cfg.model_full_path(),
            model_id=mdef.name,
            owner_id=Config.get_app_id(),
            model_type=mdef.model_type,
            variant=variant,
            keep_incumbent=pose_decoder_active,
            tunables_json=tunables,
            log=print,
        ))

    def _register_detect_model(self) -> str | None:
        """Register the secondary detection model (e.g. yolov8n) for parallel
        inference. Returns model ID or None if disabled/failed.

        Since 0.1.2 the platform may preload this model from the spec.models
        AMPK bundle (profile hailo_yolov8n_384_640, backend_function
        hailo_yolov8n) — in that case the preloaded registration is kept.
        """
        ddef = self.cfg.detect_model_def()
        if ddef is None:
            return None
        if not self.cfg.enable_parallel_infer:
            return None
        try:
            # Fast path: skip when already registered (preload or a previous
            # app restart before an ai-runtime restart wiped models).
            existing = self.infer.list_models()
            for m in existing:
                if m.model_id == ddef.name:
                    print(f"[app] detect model already registered: {ddef.name}")
                    return ddef.name
            # Empty variant → ai-runtime auto-discovers the postprocess config
            # from the HEF's sidecar json (hailo_yolov8n_384_640.json). A bare
            # name like "yolov8n" overrides discovery and ai-runtime wraps it as
            # {"backend_function":"yolov8n"} which is schema-invalid for the YOLO
            # plugin (HAILO_INVALID_OPERATION). Mirrors model-showcase main.py:2006
            # which passes None for the same HEF. postprocess_json wins when
            # present (future custom-nms models like yolov5m_vehicles).
            return ensure_model(self.infer, RegistrationSpec(
                model_path=self.cfg.detect_model_full_path(),
                model_id=ddef.name,
                owner_id=Config.get_app_id(),
                model_type=ddef.model_type,
                variant=(ddef.postprocess_json or None),
                keep_incumbent=backend_function("hailo_yolov8n"),
                log=print,
            ))
        except Exception as e:  # noqa: BLE001
            print(f"[app] detect model register failed (parallel infer disabled): {e}")
            return None

    # ---- infer loop ----
    def _evict_stale_counters(self, now: float, active_keys: set[str]) -> None:
        for k in list(self._counter_last_seen):
            if k not in active_keys and (now - self._counter_last_seen[k]) > _TRACKER_TTL_SECONDS:
                self._counters.pop(k, None)
                self._counter_last_seen.pop(k, None)
                self.alerts.drop_tracker(k)

    def _zone_id_of(self, kp) -> str | None:
        bc = body_center(kp)
        if bc is None:
            return None
        for z in self.zones.zones:
            if point_in_polygon(bc.x, bc.y, z.polygon):
                return z.id
        return None

    def _infer_loop(self) -> None:
        while self._running.is_set():
            try:
                if self._model_id is None:
                    self._model_id = self._register_model()
            except Exception as e:  # noqa: BLE001
                self._register_failures += 1
                if self._register_failures >= MODEL_REGISTER_MAX_FAILURES:
                    # Bounded retry (0.1.2): stop hiding a dead core behind a
                    # healthy web shell — fail the healthcheck so the platform
                    # restarts the container instead of us looping forever.
                    self._fatal_error = (
                        f"model registration failed "
                        f"{self._register_failures}x: {e}")
                    print(f"[app] FATAL: {self._fatal_error}; infer loop "
                          "stopped (healthcheck will fail -> container "
                          "restart)")
                    self._running.clear()
                    return
                print(f"[app] model register failed "
                      f"({self._register_failures}/"
                      f"{MODEL_REGISTER_MAX_FAILURES}): {e}; retry in 5s")
                time.sleep(5)
                continue
            self._register_failures = 0
            print(f"[app] model registered id={self._model_id}")
            # Register secondary detection model for parallel inference
            if self._detect_model_id is None and self.cfg.detect_model:
                try:
                    self._detect_model_id = self._register_detect_model()
                except Exception as e:  # noqa: BLE001
                    print(f"[app] detect model register failed: {e}")
            try:
                # An uploaded video source (set by /api/video/upload) routes
                # frames through single-frame infer() instead of the live
                # subscribe() stream — so the uploaded clip is actually
                # inferred, not merely used as an overlay backdrop.
                if self._video_source is not None:
                    self._run_video_inference()
                else:
                    self._run_live_inference()
            except Exception as e:  # noqa: BLE001 — stall recovery
                self._consec_fail += 1
                # ai-runtime keeps models in-memory only; a service restart wipes
                # the models_ map (see memory [[ai-runtime-model-registration]]).
                # The app held a stale _model_id from the pre-restart instance, so
                # StreamInfer returns NOT_FOUND "Model not found" forever. Reset
                # so the next loop iteration re-registers against the live runtime.
                msg = str(e)
                if "Model not found" in msg or "NOT_FOUND" in msg:
                    if self._model_id is not None:
                        print(f"[app] model vanished (ai-runtime restart?); "
                              f"will re-register id={self._model_id}")
                        self._model_id = None
                    self._detect_model_id = None  # also reset detect model
                    self._consec_fail = 0
                    self._register_failures = 0
                    time.sleep(2)
                    continue
                if self._consec_fail >= DEGRADED_FAILURE_THRESHOLD:
                    print(f"[app] infer degraded after {self._consec_fail} failures: {e}")
                else:
                    print(f"[app] infer transient failure #{self._consec_fail}: {e}")
                time.sleep(1)
            finally:
                self._warmup_left = INFER_WARMUP_COUNT

    def _run_live_inference(self) -> None:
        """Live depth-N async inference pipeline.

        subscribe() (the NV12 DMA-BUF zero-copy stream) feeds the raw sub
        stream frame dimensions (1280x720) straight into the HEF with no
        HAL-side resize. yolov8s_pose is compiled for a 640x640 input and
        rejects the 1280x720 NV12 tensor — HAL_ERR_INVALID_ARG (-2814) on
        every frame — so the SDK generator (inference.py: skip-on-failure)
        yields nothing and the preview stays black. Instead pull frames
        ourselves via a dedicated FdMediaClient, cv2.resize to the HEF's
        640x640 RGB input, and call single-shot infer() — the same path
        model-showcase uses (main.py:2762) and the same path the uploaded
        video mode below already uses successfully.

        Pipeline (cfg.pipeline_depth, default 2): keep N frames in-flight on
        the NPU. Each iteration pops the OLDEST submitted future, awaits it,
        and runs _on_infer — strictly in FIFO submission order, so the
        tracker/counters/SSE see frames in temporal order (no reorder buffer;
        _on_infer stays single-threaded). While frame N is awaited +
        postprocessed, frames N+1..N+depth-1 execute on the NPU, so the NPU
        stays busy across the host-side gap that idled it under the old serial
        loop. pipeline_depth=1 reproduces the legacy serial behavior (kill-
        switch). The SDK's infer_async/infer_batch_async return futures on the
        same asyncio loop infer() uses; ai-runtime services concurrent same-
        model jobs via its 4-worker scheduler + the submit_mtx overlap fix (no
        per-session in-flight cap is enforced in the C++ layer).
        """
        stream_id = self.cfg.infer_stream_id
        depth = max(1, self.cfg.pipeline_depth)
        print(f"[app] live infer start stream={stream_id} "
              f"fps={self.cfg.infer_fps} depth={depth}")
        target_dt = 1.0 / max(self.cfg.infer_fps, 5)

        def _submit_next():
            """Fetch + prepare + submit ONE frame via the async SDK path.

            Returns ``(fut, is_batch, sync_meta, req_timeout)`` or None when
            no frame is available (caller sleeps + retries). Warmup timeout
            accounting happens here, per submitted frame. is_batch selects the
            result-unpacking branch in the awaiter (batch -> List result,
            pose-only -> single InferenceResult).
            """
            try:
                frame = self._get_latest_frame(
                    self.media_infer, stream_id, timeout_ms=3000)
            except Exception as e:  # noqa: BLE001
                print(f"[app] live infer get_frame failed: {e}")
                return None
            if frame is None:
                return None
            bgr = cv2.cvtColor(frame.to_rgb(), cv2.COLOR_RGB2BGR)
            inp = self._prepare_infer_input(bgr)

            req_timeout = (INFER_WARMUP_TIMEOUT_MS if self._warmup_left > 0
                           else INFER_TIMEOUT_MS)
            if self._warmup_left > 0:
                self._warmup_left -= 1

            # Parallel inference: pose runs every frame; the secondary detect
            # model is decimated to every Nth frame (cfg.detect_every). On
            # detect frames submit pose+detect in one infer_batch_async(); on
            # skip frames submit pose-only infer_async() and reuse the most-
            # recent AWAITED detection — correct because we await in FIFO
            # order, so any earlier detect frame has updated
            # _last_detect_result by the time this skip frame is awaited.
            self._infer_frame_seq += 1
            every = max(self.cfg.detect_every, 1)
            parallel = (self._detect_model_id is not None
                        and self.cfg.enable_parallel_infer)
            run_detect = parallel and self._infer_frame_seq % every == 0

            sync_meta = {
                "frame_sequence": int(getattr(frame, "sequence", 0)),
                "frame_timestamp_ns": int(getattr(frame, "timestamp_ns", 0)),
                "width": int(getattr(frame, "width", bgr.shape[1])),
                "height": int(getattr(frame, "height", bgr.shape[0])),
            }
            if sync_meta["frame_timestamp_ns"] > 0:
                sync_meta["video_pts90"] = (
                    sync_meta["frame_timestamp_ns"] * 90000
                    // 1_000_000_000) & 0xFFFFFFFF

            if run_detect:
                detect_inp = self._prepare_detect_input(bgr)
                items = [
                    BatchInferItem(image=inp, model_id=self._model_id,
                                   timeout_ms=req_timeout),
                    BatchInferItem(image=detect_inp,
                                   model_id=self._detect_model_id,
                                   timeout_ms=req_timeout),
                ]
                fut = self.infer.infer_batch_async(
                    items, timeout_ms=req_timeout * 2)
                return (fut, True, sync_meta, req_timeout)
            fut = self.infer.infer_async(
                inp, self._model_id, timeout_ms=req_timeout)
            return (fut, False, sync_meta, req_timeout)

        # Sliding window of in-flight futures, oldest at the left. Each entry:
        # (fut, is_batch, sync_meta, req_timeout).
        in_flight: deque = deque()

        # Prime up to `depth` frames so the NPU has a backlog before the first
        # await.
        while self._running.is_set() and len(in_flight) < depth:
            if self._video_source is not None:
                return
            entry = _submit_next()
            if entry is None:
                time.sleep(0.05)
            else:
                in_flight.append(entry)

        # Per-segment latency profile, logged every 10 OUTPUT frames.
        # submit = fetch+prep+async-submit of the NEXT frame (host work that
        #   overlaps with the NPU running the in-flight backlog);
        # await = wait on the OLDEST future (the pipeline critical path —
        #   small when the NPU keeps up, large when NPU-bound);
        # post = _on_infer (postprocess + snapshot + publish);
        # iter = submit + await + post (host thread per-output wall time);
        # queue = frames still in-flight at profile time.
        # get_stats() ms is filled by _on_infer.
        prof_n = 0
        prof = {"submit": 0.0, "await": 0.0, "post": 0.0,
                "stats": 0.0, "iter": 0.0, "queue": 0.0}

        while self._running.is_set():
            # a video upload mid-stream switches us to single-frame infer()
            if self._video_source is not None:
                return
            if not in_flight:
                # all entries dropped (e.g. a run of get_frame failures) —
                # re-prime one so the pipeline never stalls.
                entry = _submit_next()
                if entry is None:
                    time.sleep(0.05)
                    continue
                in_flight.append(entry)
                continue

            t0 = time.perf_counter()
            fut, is_batch, sync_meta, req_timeout = in_flight.popleft()
            # Refill immediately: prep + submit the next frame on the host
            # thread NOW so the NPU sees it ASAP and the window stays full
            # during the await + postprocess below. This host-side prep
            # overlaps with the NPU executing the still-in-flight backlog.
            nxt = _submit_next()
            if nxt is not None:
                in_flight.append(nxt)
            t_submit = time.perf_counter()

            try:
                if is_batch:
                    results = fut.result(timeout=req_timeout / 1000 + 5)
                    result = results[0]
                    detect_result = (results[1] if len(results) > 1
                                     else None)
                    self._last_detect_result = detect_result
                else:
                    result = fut.result(timeout=req_timeout / 1000 + 5)
                    detect_result = self._last_detect_result
            except Exception as e:  # noqa: BLE001
                self._consec_fail += 1
                if self._consec_fail >= DEGRADED_FAILURE_THRESHOLD:
                    print(f"[app] infer degraded after "
                          f"{self._consec_fail} failures: {e}")
                else:
                    print(f"[app] infer transient failure #"
                          f"{self._consec_fail}: {e}")
                # drop this frame; the refill above already backfills the
                # window so the pipeline keeps moving.
                self._prof_get_stats_ms = 0.0
                continue
            t_await = time.perf_counter()

            self._on_infer(
                result, detect_result=detect_result, sync_meta=sync_meta)
            t_post = time.perf_counter()
            self._consec_fail = 0

            prof_n += 1
            prof["submit"] += (t_submit - t0) * 1000.0
            prof["await"] += (t_await - t_submit) * 1000.0
            prof["post"] += (t_post - t_await) * 1000.0
            prof["stats"] += self._prof_get_stats_ms
            prof["iter"] += (t_post - t0) * 1000.0
            prof["queue"] += len(in_flight)
            self._prof_get_stats_ms = 0.0
            if prof_n >= 10:
                a = lambda k: prof[k] / prof_n
                # iter = submit + await + post on the host thread. If the host
                # can't drain faster than iter, that's the output cap; if
                # await dominates, the NPU (not the host) is the limit.
                eff = max(a("iter"), target_dt * 1000.0)
                print(
                    f"[prof] n={prof_n} iter={a('iter'):.0f}ms "
                    f"(host-ceiling {1000 / eff:.1f}Hz) "
                    f"submit={a('submit'):.0f} await={a('await'):.0f} "
                    f"post={a('post'):.0f} queue={a('queue'):.1f} "
                    f"[stats={a('stats'):.0f}]")
                prof_n = 0
                for k in prof:
                    prof[k] = 0.0
            # rate-limit to target_dt TOTAL cycle time (work + sleep). The NPU
            # keeps running the in-flight backlog during this sleep, so it no
            # longer idles the way the serial loop's additive sleep did.
            time.sleep(max(0.0, target_dt - (time.perf_counter() - t0)))

    def _get_latest_frame(self, media: FdMediaClient, stream_id: str,
                          timeout_ms: int = 3000):
        """Read one frame, then drain queued frames so inference stays live.

        The raw-frame socket is FIFO. When inference runs slower than the
        camera stream, consuming one frame per infer loop makes pose metadata
        several seconds older than the H.264 preview. Draining without color
        conversion keeps only the freshest frame and prevents that backlog.
        """
        frame = media.get_frame(stream_id, timeout_ms=timeout_ms)
        if frame is None:
            return None
        deadline = time.monotonic() + 0.12
        drained = 0
        while drained < 120 and time.monotonic() < deadline:
            nxt = media.get_frame(stream_id, timeout_ms=1)
            if nxt is None:
                break
            frame = nxt
            drained += 1
        return frame

    def _run_video_inference(self) -> None:
        src = self._video_source
        if src is None:
            return
        # new clip → clear stale cross-mode track assignments
        self._tracker.reset()
        print(f"[app] video mode: inferring uploaded clip {src.path} "
              f"@ {src.fps:.1f}fps")
        for frame in src.frames():
            if src is not self._video_source or not self._running.is_set():
                return
            inp = self._prepare_infer_input(frame)
            timeout = (INFER_WARMUP_TIMEOUT_MS if self._warmup_left > 0
                       else INFER_TIMEOUT_MS)
            detect_result = None
            if (self._detect_model_id is not None
                    and self.cfg.enable_parallel_infer):
                detect_inp = self._prepare_detect_input(frame)
                items = [
                    BatchInferItem(image=inp, model_id=self._model_id,
                                   timeout_ms=timeout),
                    BatchInferItem(image=detect_inp,
                                   model_id=self._detect_model_id,
                                   timeout_ms=timeout),
                ]
                results = self.infer.infer_batch(
                    items, timeout_ms=timeout * 2)
                result = results[0]
                detect_result = results[1] if len(results) > 1 else None
            else:
                result = self.infer.infer(
                    inp, self._model_id, timeout_ms=timeout)
            if self._warmup_left > 0:
                self._warmup_left -= 1
            self._on_infer(result, bg_frame=frame,
                           detect_result=detect_result)
            self._consec_fail = 0

    def _prepare_infer_input(self, bgr: np.ndarray) -> np.ndarray:
        """Resize BGR frame to the model input, return flattened RGB uint8.

        The SDK's infer() path sends raw tensor bytes (no HAL-side resize,
        unlike the NV12 subscribe() stream), so the caller must match the
        HEF's expected 640×640 RGB input — same convention as model-showcase
        _prepare_stage_input (main.py:2310).
        """
        w, h = self.cfg.model_def().input_size
        if bgr.shape[1] != w or bgr.shape[0] != h:
            bgr = cv2.resize(bgr, (w, h), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        return rgb.flatten()

    def _prepare_detect_input(self, bgr: np.ndarray) -> np.ndarray:
        """Resize BGR frame and convert to NV12 (YUV420SP) for detection models.

        Detection HEFs (yolov8n, yolov5m_vehicles, etc.) expect NV12 input,
        NOT RGB. The conversion follows the same path as the parallel inference
        test script: BGR→RGB→YUV_I420→interleave U/V→NV12 flattened.
        """
        ddef = self.cfg.detect_model_def()
        if ddef is None:
            w, h = 640, 640
        else:
            w, h = ddef.input_size
        if bgr.shape[1] != w or bgr.shape[0] != h:
            bgr = cv2.resize(bgr, (w, h), interpolation=cv2.INTER_LINEAR)
        rgb = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        # RGB → YUV420 (I420 planar) → NV12 (Y + interleaved UV)
        yuv = cv2.cvtColor(rgb, cv2.COLOR_RGB2YUV_I420)
        y_plane = yuv[:h]
        u_plane = yuv[h:h + h // 4]
        v_plane = yuv[h + h // 4:]
        uv_nv12 = np.empty((h // 2 * w), dtype=np.uint8)
        uv_nv12[0::2] = u_plane.flatten()
        uv_nv12[1::2] = v_plane.flatten()
        return np.concatenate([y_plane.flatten(), uv_nv12]).reshape(
            int(h * 1.5), w)

    def _on_infer(self, result, bg_frame: np.ndarray | None = None,
                  detect_result=None,
                  sync_meta: dict | None = None) -> None:
        now = time.time()
        # measured pose completions/sec over a trailing window — the real
        # overlay update rate clients see, vs the static cfg.infer_fps cap.
        self._infer_ts.append(now)
        cutoff = now - 4.0
        pose_fps = sum(1 for t in self._infer_ts if t >= cutoff) / 4.0
        persons: list[tuple[str, object]] = []
        snapshots: list[dict] = []
        active_keys: set[str] = set()

        # Stable per-person ids via body-center nearest-neighbor association.
        # Replaces the positional `p{i}` scheme that swapped identities when
        # landmark ordering shifted between frames.
        kp_sets = [parse_keypoints(lm_set.points, self.cfg.keypoint_threshold)
                   for lm_set in result.landmarks]
        ids = self._tracker.update(kp_sets)

        for kp, key in zip(kp_sets, ids):
            if key is None:
                continue
            active_keys.add(key)
            counter = self._counters.get(key)
            if counter is None:
                # Auto-detect exercise from zone context or pose heuristics
                zid = self._zone_id_of(kp)
                exercise = self.exercise_classifier.classify(kp, zone_id=zid)
                try:
                    counter = make_counter(exercise, now)
                except ValueError:
                    counter = make_counter(_EXERCISE, now)  # fallback to default
                self._counters[key] = counter
            self._counter_last_seen[key] = now
            counter.update(kp, now)
            persons.append((key, kp))
            mid = self._member_ids.get(key)
            snapshots.append({
                "id": key,
                "snap": counter.snapshot(),
                "member_id": mid,
                # Preserve COCO-17 indexes for the browser skeleton table.
                # Missing/low-confidence points stay as null instead of
                # compacting the array and corrupting edge indexes.
                "landmarks": [
                    {"x": p.x, "y": p.y, "c": p.conf} if p else None
                    for p in kp
                ],
            })

        self._evict_stale_counters(now, active_keys)

        # --- Secondary detection model results (person bboxes) ---
        # Bboxes supplement keypoint-based zone membership and fall detection.
        # When keypoints are missing/low-confidence (occluded person), the bbox
        # center still provides a zone membership signal.
        detect_persons: list[dict] = []  # [{x, y, w, h, score, aspect_ratio}]
        if detect_result is not None:
            for obj in detect_result.get_objects_by_label("person"):
                bbox = obj.bbox
                aspect = bbox.width / max(bbox.height, 1e-6)
                cx = bbox.x + bbox.width / 2
                cy = bbox.y + bbox.height / 2
                detect_persons.append({
                    "x": cx, "y": cy,
                    "w": bbox.width, "h": bbox.height,
                    "score": obj.score,
                    "aspect_ratio": aspect,
                })

        zone_snap = self.zones.update(persons, now, detect_persons=detect_persons,
                                       member_ids=self._member_ids)
        zone_names = {z.id: z.name for z in self.zones.zones}

        # Trajectory: aggregate zone assignments into visits/sessions.
        trajectory_events: list[dict] = []
        if self.trajectory is not None:
            counter_snaps = {p["id"]: p["snap"] for p in snapshots}
            trajectory_events = self.trajectory.update(
                persons, zone_snap.get("assignments", {}),
                counter_snaps, self._member_ids, now)
            for ev in trajectory_events:
                if ev["type"] == "visit_confirmed":
                    if self.cfg.trajectory_rebind_counter:
                        self._rebind_counter_on_visit(ev, now)
                elif ev["type"] == "session_summary":
                    self._publish_trajectory_summary(ev)

        alert_events: list[dict] = []
        for key, kp in persons:
            zid = self._zone_id_of(kp)
            # Find matching detection bbox aspect ratio for fall detection
            bbox_aspect = None
            if detect_persons:
                # Match by proximity: find detect bbox closest to this person's
                # body center
                bc = body_center(kp)
                if bc is not None:
                    best_dist = float("inf")
                    for dp in detect_persons:
                        dist = ((dp["x"] - bc.x) ** 2
                                + (dp["y"] - bc.y) ** 2)
                        if dist < best_dist:
                            best_dist = dist
                            bbox_aspect = dp["aspect_ratio"]
            alert_events += self.alerts.evaluate_tracker(
                key, kp, now, zid, self.forbidden_zones,
                bbox_aspect_ratio=bbox_aspect)
        alert_events += self.alerts.evaluate_crowd(
            zone_snap["crowded"], zone_snap["counts"], zone_names, now)
        alert_events += self.alerts.forward_equipment(zone_snap["events"], now)
        # Wire equipment stats from zone events
        for ev in zone_snap["events"]:
            etype = ev.get("type", "")
            eid = ev.get("equipment_id", "")
            if etype == "equipment_occupy":
                self.equipment_stats.on_bind(eid, ev.get("member_id"), now)
            elif etype == "equipment_release":
                self.equipment_stats.on_unbind(eid, now)
        alert_events += self.alerts.evaluate_after_hours(
            self.cfg.after_hours, zone_snap["total"], now)

        # device_utilization is sampled by a background thread
        # (_stats_sampler_loop) because get_stats() is a ~0.5s RPC that
        # dominated the inference loop. The hot loop just reads the cached
        # value (zero cost).
        du = self._stats_last_du
        stats = {}
        hw_us = getattr(result, "hw_infer_time_us", 0)
        fps = self.cfg.infer_fps

        # publish the latest overlay context for SSE/client-side drawing and
        # for the uploaded-video overlay path.
        with self._overlay_lock:
            # keep prior frame for velocity-based interpolation; stamp last
            # with the inference time so the preview thread can extrapolate.
            self._prev_overlay = self._last_overlay
            self._last_overlay = (persons, snapshots, zone_snap, fps, now)

        # video mode bakes the overlay onto the uploaded frame directly; live
        # mode leaves it to the preview thread.
        if bg_frame is not None:
            self._render_overlay(bg_frame, persons, snapshots, zone_snap, fps)

        snapshot = {
            "ts": now,
            "persons": snapshots,
            "zones": zone_snap,
            "alerts": alert_events,
            "equipment_stats": self.equipment_stats.snapshot(now),
            "member_ids": dict(self._member_ids),
            "hw_infer_time_us": hw_us,
            "device_utilization": du,
            "video_mode": self._video_mode,
            "fps": fps,
            "pose_fps": round(pose_fps, 1),
            "detect_persons": len(detect_persons),
            "parallel_infer": self._detect_model_id is not None,
            "trajectory": {
                "enabled": self.trajectory is not None,
                "active": (self.trajectory.active_snapshot(now)
                           if self.trajectory else []),
                "trails": (self.trajectory.trails_snapshot()
                           if self.trajectory else {}),
                # session summaries closed on this frame (for UI timeline)
                "events": [e for e in trajectory_events
                           if e["type"] == "session_summary"],
            },
        }
        with self._state_lock:
            self._latest = snapshot
        if sync_meta is not None and not self._video_mode:
            self._publish_sync_preview(snapshot, sync_meta)
        self._broadcast_sse(snapshot)
        self._publish_periodic(now, snapshots, zone_snap)

    def _rebind_counter_on_visit(self, ev: dict, now: float) -> None:
        """On visit_confirmed, rebuild the counter if its exercise no longer
        matches the zone's equipment. Default ON (TRAJECTORY_REBIND_COUNTER).

        The trajectory module captures its counter baseline lazily on the
        FIRST frame after confirmation where the snapshot exercise matches,
        so a same-frame rebuild here does not corrupt rep attribution.
        """
        key = ev.get("tracker_id")
        exercise = ev.get("exercise")
        if not key or not exercise:
            return
        counter = self._counters.get(key)
        if counter is not None and getattr(counter, "exercise", None) == exercise:
            return
        try:
            self._counters[key] = make_counter(exercise, now)
            print(f"[trajectory] counter rebound: {key} -> {exercise}")
        except ValueError:
            pass  # unknown exercise: keep the existing counter

    def _publish_trajectory_summary(self, payload: dict) -> None:
        """Push a closed-session summary to the gym/trajectory bus topic."""
        try:
            self.events.publish(TOPIC_TRAJECTORY, payload)
        except Exception as e:  # noqa: BLE001
            print(f"[trajectory] WARN: publish failed: {e}")

    def _publish_periodic(self, now: float, snapshots: list[dict],
                          zone_snap: dict) -> None:
        """Throttled gym/pose + gym/occupancy snapshots on the event bus.

        These topics were declared in app.yaml but never published; the
        event-driven gym/equipment + gym/alerts paths stay as-is. Periodic
        snapshots let external subscribers (dashboard, logger) follow pose
        counts and occupancy without scraping /api/state.
        """
        if (now - self._last_event_ts) < _EVENT_SNAPSHOT_SECONDS:
            return
        self._last_event_ts = now
        try:
            self.events.publish(TOPIC_POSE, {
                "ts": now,
                "persons": [{"id": s["id"],
                             "reps": s["snap"].get("reps", 0),
                             "phase": s["snap"].get("phase")}
                            for s in snapshots],
            })
            self.events.publish(TOPIC_OCCUPANCY, {
                "ts": now,
                "total": zone_snap["total"],
                "counts": zone_snap["counts"],
                "crowd": list(zone_snap["crowded"]),
            })
            # Publish identity events for recognized members
            if self._member_ids:
                self.events.publish(TOPIC_IDENTITY, {
                    "ts": now,
                    "members": [{"tracker_id": tid, "member_id": mid}
                                for tid, mid in self._member_ids.items()],
                })
        except Exception as e:  # noqa: BLE001 — bus must not break infer loop
            print(f"[app] periodic event publish failed: {e}")

    def _render_overlay(self, frame: np.ndarray, persons, snapshots,
                       zone_snap: dict, fps) -> None:
        """Draw zones + skeleton + per-person tags + status bar onto a BGR
        frame, then JPEG-encode it into self._latest_jpeg. Shared by the
        uploaded-video infer path and the live MJPEG preview thread."""
        overlay.draw_zones(frame, self.zones.zones, set(zone_snap["crowded"]),
                           counts=zone_snap["counts"])
        # motion trails (video mode renders server-side; live mode draws
        # them in the browser from snapshot["trajectory"]["trails"])
        if self.trajectory is not None:
            for _tid, pts in self.trajectory.trails_snapshot().items():
                overlay.draw_trail(frame, pts)
        for _key, kp in persons:
            overlay.draw_keypoints(frame, kp)
        y = 24
        for snap in snapshots:
            y = overlay.draw_exercise_tag(frame, snap["id"], snap["snap"], y,
                                          member_id=snap.get("member_id"))
        overlay.draw_status_bar(frame, zone_snap["total"],
                                zone_snap["counts"], fps)
        ok, buf = cv2.imencode(".jpg", frame,
                               [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if ok:
            with self._jpeg_lock:
                self._latest_jpeg = buf.tobytes()

    def _publish_sync_preview(self, snapshot: dict, sync_meta: dict) -> None:
        """Publish pose metadata for the H.264 preview overlay.

        The browser receives video through the platform hardware H.264 stream.
        This websocket only carries the inference snapshot and source frame
        timing/sequence metadata, avoiding per-frame JPEG encode/decode cost.
        """
        with self._sync_cond:
            self._sync_seq += 1
            meta = {
                "type": "pose_sync",
                "seq": self._sync_seq,
                "ts": snapshot.get("ts"),
                "snapshot": snapshot,
            }
            meta.update(sync_meta)
            packet = json.dumps(meta, separators=(",", ":"), default=str)
            self._latest_sync_packet = (self._sync_seq, packet)
            self._sync_cond.notify_all()

    def _interp_persons(self, prev: tuple | None, last: tuple,
                        tp: float) -> list[tuple[str, KeyPoints]]:
        """Linearly extrapolate keypoints by velocity so the skeleton tracks
        the person between inference frames (preview runs faster than infer).

        prev/last = (persons, snapshots, zone_snap, fps, ts). Falls back to
        last.persons when: no prev, predict horizon exceeded, or infer
        interval too small. Displacement is clamped to bound overshoot on
        sudden stops/turns; None keypoints stay None; unmatched (first-seen)
        persons use last as-is.
        """
        last_persons: list[tuple[str, KeyPoints]] = last[0]
        if prev is None:
            return last_persons
        ts_prev: float = prev[4]
        ts_last: float = last[4]
        dt_infer = ts_last - ts_prev
        tp_age = tp - ts_last
        if tp_age > _MAX_PREDICT_HORIZON or dt_infer < _MIN_INFER_DT:
            return last_persons
        # index prev keypoints by tracker id for O(1) pairing
        prev_by_id: dict[str, KeyPoints] = {
            tid: kp for tid, kp in prev[0]
        }
        out: list[tuple[str, KeyPoints]] = []
        for tid, kp_last in last_persons:
            kp_prev = prev_by_id.get(tid)
            if kp_prev is None:
                out.append((tid, kp_last))
                continue
            interp: KeyPoints = []
            for p_last, p_prev in zip(kp_last, kp_prev):
                if p_last is None or p_prev is None:
                    interp.append(p_last)
                    continue
                vx = (p_last.x - p_prev.x) / dt_infer
                vy = (p_last.y - p_prev.y) / dt_infer
                x_i = p_last.x + vx * tp_age
                y_i = p_last.y + vy * tp_age
                # clamp displacement to bound overshoot on stops/turns
                dx = x_i - p_last.x
                if dx > _MAX_EXTRAP_DISPL:
                    x_i = p_last.x + _MAX_EXTRAP_DISPL
                elif dx < -_MAX_EXTRAP_DISPL:
                    x_i = p_last.x - _MAX_EXTRAP_DISPL
                dy = y_i - p_last.y
                if dy > _MAX_EXTRAP_DISPL:
                    y_i = p_last.y + _MAX_EXTRAP_DISPL
                elif dy < -_MAX_EXTRAP_DISPL:
                    y_i = p_last.y - _MAX_EXTRAP_DISPL
                interp.append(Pt(x_i, y_i, p_last.conf))
            out.append((tid, interp))
        return out

    def _preview_loop(self) -> None:
        """Live MJPEG preview: pull raw frames from FdMediaClient on the
        preview stream and JPEG-encode them as bare frames (no overlay).

        Overlay (skeleton, zones, tags) is rendered client-side on a Canvas
        via SSE landmarks + requestAnimationFrame interpolation. This
        eliminates the MJPEG encode/decode latency for overlay and allows
        60 fps client-side interpolation vs the 25 fps server-side path.

        Video-upload mode bakes its own JPEGs in _on_infer; stay idle so
        we don't clobber them with a stale live frame.
        """
        stream_id = self.cfg.stream_id
        print(f"[app] preview thread start stream={stream_id}")
        target_dt = 1.0 / max(self.cfg.preview_fps, 5)
        while self._running.is_set():
            if self._video_mode:
                time.sleep(0.2)
                continue
            with self._stream_clients_lock:
                has_clients = self._stream_clients > 0
            if not has_clients:
                time.sleep(0.2)
                continue
            t0 = time.time()
            try:
                frame = self.media.get_frame(stream_id, timeout_ms=3000)
            except Exception as e:  # noqa: BLE001 — transient media errors
                print(f"[app] preview get_frame failed: {e}")
                time.sleep(0.5)
                continue
            if frame is None:
                time.sleep(0.1)
                continue
            try:
                bgr = cv2.cvtColor(frame.to_rgb(), cv2.COLOR_RGB2BGR)
            except Exception as e:  # noqa: BLE001 — bad frame, skip
                time.sleep(0.05)
                continue
            # JPEG-encode bare frame; overlay is drawn client-side
            ok, buf = cv2.imencode(".jpg", bgr,
                                   [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok:
                with self._jpeg_lock:
                    self._latest_jpeg = buf.tobytes()
            # throttle to target preview fps
            dt = time.time() - t0
            if dt < target_dt:
                time.sleep(target_dt - dt)

    # ---- SSE ----
    def _broadcast_sse(self, snap: dict) -> None:
        data = json.dumps(snap, default=str)
        with self._sse_lock:
            dead: list[queue.Queue] = []
            for q in self._sse_clients:
                try:
                    q.put_nowait(data)
                except queue.Full:
                    dead.append(q)
            for d in dead:
                self._sse_clients.remove(d)

    # ---- routes ----
    def _register_routes(self) -> None:
        self.app.add_url_rule("/api/health", "health", self._health)
        self.app.add_url_rule("/api/config", "config", self._config)
        self.app.add_url_rule("/", "index", self._index)
        self.app.add_url_rule("/stream", "stream", self._stream)
        self.app.add_url_rule("/api/state", "state", self._state)
        self.app.add_url_rule("/api/events", "events", self._events)
        self.app.add_url_rule("/api/video/upload", "video_upload",
                              self._video_upload, methods=["POST"])
        self.app.add_url_rule("/api/video/control", "video_control",
                              self._video_control, methods=["POST"])
        self.app.add_url_rule("/api/preview", "preview", self._preview)
        self.app.add_url_rule("/api/trajectory/active", "trajectory_active",
                              self._trajectory_active)
        self.app.add_url_rule("/api/trajectory/sessions", "trajectory_sessions",
                              self._trajectory_sessions)
        self.app.add_url_rule("/api/trajectory/sessions/<path:session_id>",
                              "trajectory_session", self._trajectory_session)
        self.sock.route("/ws/sync-preview")(self._sync_preview_ws)

    def _health(self):
        # 503 on fatal so the container healthcheck (curl -f) actually fails
        # and the platform restarts us — a 200 "ok:false" would be invisible.
        if self._fatal_error is not None:
            return jsonify({"ok": False, "app": "gym-ops",
                            "error": self._fatal_error}), 503
        return jsonify({"ok": True, "app": "gym-ops", "model": self._model_id,
                        "video_mode": self._video_mode})

    def _trajectory_active(self):
        """Live in-gym sessions: current zone, dwell, overlay trails."""
        if self.trajectory is None:
            return jsonify({"enabled": False, "active": [], "trails": {}})
        return jsonify({
            "enabled": True,
            "active": self.trajectory.active_snapshot(time.time()),
            "trails": self.trajectory.trails_snapshot(),
        })

    def _trajectory_sessions(self):
        """Persisted session summaries, newest first. ?member_id= filter
        only returns rows when identity linking was enabled at write time."""
        if self.trajectory_store is None:
            return jsonify({"enabled": False, "sessions": []})
        limit = request.args.get("limit", default=50, type=int)
        member_id = request.args.get("member_id") or None
        return jsonify({
            "enabled": True,
            "sessions": self.trajectory_store.load_sessions(
                limit=limit, member_id=member_id),
        })

    def _trajectory_session(self, session_id: str):
        """One persisted session summary by id."""
        if self.trajectory_store is None:
            return jsonify({"error": "trajectory persistence disabled"}), 404
        session = self.trajectory_store.get_session(session_id)
        if session is None:
            return jsonify({"error": "session not found"}), 404
        return jsonify(session)

    def _config(self):
        """Zone/equipment definitions for the UI (names + capacities + polygons)."""
        return jsonify({
            "zones": [{"id": z.id, "name": z.name, "capacity": z.capacity,
                       "polygon": z.polygon, "color": z.color,
                       "forbidden": z.forbidden}
                      for z in self.zones.zones],
            "equipment": [{"id": e.id, "name": e.name, "zone_id": e.zone_id}
                          for e in self.zones.equipment],
            "exercise": _EXERCISE,
        })

    def _index(self):
        return render_template("index.html",
                               cfg=self.cfg, exercise=_EXERCISE,
                               ws_scheme=self.cfg.platform_api_ws_scheme,
                               ws_port=self.cfg.platform_api_port)

    def _stream(self):
        # Optional ?fps=N throttle (clamped 1..10) for thin remote paths, e.g.
        # after the frontend auto-degrades HD→MJPEG over a relay tunnel.
        # Without the param the loop runs at cfg.preview_fps (LAN behavior).
        try:
            req_fps = int(request.args.get("fps", 0))
        except (TypeError, ValueError):
            req_fps = 0
        frame_interval = 1.0 / req_fps if 1 <= req_fps <= 10 else 1.0 / max(self.cfg.preview_fps, 5)

        def gen():
            boundary = b"--frame\r\n"
            with self._stream_clients_lock:
                self._stream_clients += 1
            try:
                while self._running.is_set():
                    with self._jpeg_lock:
                        jpg = self._latest_jpeg
                    if jpg is None:
                        time.sleep(0.05)
                        continue
                    yield (boundary + b"Content-Type: image/jpeg\r\n\r\n" + jpg + b"\r\n")
                    time.sleep(frame_interval)
            finally:
                with self._stream_clients_lock:
                    self._stream_clients = max(0, self._stream_clients - 1)
        return Response(gen(), mimetype="multipart/x-mixed-replace; boundary=frame")

    def _sync_preview_ws(self, ws) -> None:
        last_seq = 0
        sent = 0
        try:
            while self._running.is_set():
                with self._sync_cond:
                    self._sync_cond.wait_for(
                        lambda: (
                            self._latest_sync_packet is not None
                            and self._latest_sync_packet[0] != last_seq
                        ),
                        timeout=5.0,
                    )
                    latest = self._latest_sync_packet
                if latest is None or latest[0] == last_seq:
                    continue
                last_seq, packet = latest
                ws.send(packet)
                sent += 1
        except Exception as e:  # noqa: BLE001
            print(f"[app] sync preview ws disconnected after {sent} frames: {e}")

    def _state(self):
        with self._state_lock:
            return jsonify(self._latest)

    def _events(self):
        q: queue.Queue = queue.Queue(maxsize=64)
        with self._sse_lock:
            self._sse_clients.append(q)

        def gen():
            try:
                with self._state_lock:
                    yield f"data: {json.dumps(self._latest, default=str)}\n\n"
                while self._running.is_set():
                    try:
                        data = q.get(timeout=15)
                        yield f"data: {data}\n\n"
                    except queue.Empty:
                        yield ": keepalive\n\n"
            finally:
                with self._sse_lock:
                    if q in self._sse_clients:
                        self._sse_clients.remove(q)
        return Response(gen(), mimetype="text/event-stream")

    # ---- upload video mode ----
    def _video_upload(self):
        if "file" not in request.files:
            return jsonify({"ok": False, "error": "no file"}), 400
        f = request.files["file"]
        os.makedirs("/app/uploads", exist_ok=True)
        path = os.path.join("/app/uploads", f.filename or "clip.mp4")
        f.save(path)
        if self._video_source is not None:
            self._video_source.close()
        self._video_source = VideoFrameSource(path, target_fps=float(self.cfg.infer_fps))
        self._video_mode = True
        return jsonify({"ok": True, "path": path, "fps": self._video_source.fps})

    def _video_control(self):
        action = (request.get_json(silent=True) or {}).get("action")
        if action == "stop":
            if self._video_source is not None:
                self._video_source.close()
                self._video_source = None
            self._video_mode = False
            return jsonify({"ok": True, "video_mode": False})
        return jsonify({"ok": False, "error": "unknown action"}), 400

    def _preview(self):
        """Return platform-api H264 MSE wsUrl (token as ?token=). Mirrors
        model-showcase main.py:3994-4024.

        app-manager does not expand `${VAR}` references in the manifest and
        does not inject AIPC_TOKEN_KEY, so PLATFORM_API_TOKEN can arrive as
        the literal string `${AIPC_TOKEN_KEY}`. Detect that and report HD as
        disabled so the UI falls back to MJPEG instead of a black MSE screen.
        """
        scheme = self.cfg.platform_api_ws_scheme
        port = self.cfg.platform_api_port
        token = self.cfg.platform_api_token
        # an unexpanded `${...}` (or empty) token cannot authenticate against
        # platform-api's auth middleware → HD MSE would 401 → black screen.
        token_ok = bool(token) and "${" not in token
        host = request.host.split(":")[0] if request.host else "localhost"
        ws_url = ""
        if token_ok:
            ws_url = f"{scheme}://{host}:{port}/api/v1/h264/{self.cfg.stream_id}"
            ws_url += "?token=" + urllib.parse.quote(token, safe="")
        return jsonify({"enabled": token_ok, "wsUrl": ws_url,
                        "stream_id": self.cfg.stream_id})

    def _stats_sampler_loop(self) -> None:
        """Background device_utilization sampler.

        get_stats() is a ~0.5s RPC against ai-runtime; calling it on the
        inference loop throttled pose to ~1.5-4Hz. Sampling here on a daemon
        thread fully decouples that latency from the hot loop: _on_infer just
        reads the cached value. The SDK marshals every call onto one asyncio
        loop via run_coroutine_threadsafe, and get_stats is await-bound (not
        loop-hogging) — infer stayed ~63ms even while get_stats ran every
        frame — so the sampler and the inference worker coexist without
        serializing.
        """
        n = 0
        while self._running.is_set():
            try:
                t0 = time.perf_counter()
                stats = self.infer.get_stats()
                dt_ms = (time.perf_counter() - t0) * 1000.0
                du = stats.get("device_utilization")
                if du is not None:
                    self._stats_last_du = du
                self._stats_last_ts = time.time()
                n += 1
                if n % 10 == 0:
                    print(f"[stats] du={du} rpc={dt_ms:.0f}ms")
            except Exception as e:  # noqa: BLE001
                print(f"[app] stats sampler failed: {e}")
            time.sleep(self.cfg.stats_interval_seconds)

    # ---- lifecycle ----
    def start(self) -> None:
        try:
            AppClient().register_web_url("/")
        except Exception as e:  # noqa: BLE001
            print(f"[app] register_web_url failed: {e}")
        self._infer_thread = threading.Thread(target=self._infer_loop, daemon=True)
        self._infer_thread.start()
        self._preview_thread = threading.Thread(
            target=self._preview_loop, daemon=True)
        self._preview_thread.start()
        self._stats_thread = threading.Thread(
            target=self._stats_sampler_loop, daemon=True)
        self._stats_thread.start()
        self.app.run(host="0.0.0.0", port=self.cfg.web_port, threaded=True)

    def stop(self) -> None:
        self._running.clear()


def _handle_term(signum, _frame):
    """Translate SIGTERM into the normal shutdown path.

    The container runs this module as PID 1, and the kernel ignores
    default-disposition signals for PID 1: without an explicit handler,
    containerd's SIGTERM never reaches Python and every app-manager stop
    degrades to the full grace-timeout wait followed by SIGKILL (measured
    on device: 30s hang, exit 137). Raising SystemExit from the handler
    unwinds Flask's serve_forever select just like Ctrl-C does.
    """
    print(f"[app] received signal {signum}, shutting down", flush=True)
    raise SystemExit(0)


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_term)
    app = GymApp()
    try:
        app.start()
    except (KeyboardInterrupt, SystemExit):
        print("[app] web server stopped", flush=True)
    finally:
        # stop() clears the running flag so daemon loops wind down; exit
        # deterministically afterwards so a slow teardown can never push
        # container stop back into the kill-timeout path.
        app.stop()
        os._exit(0)


if __name__ == "__main__":
    main()
