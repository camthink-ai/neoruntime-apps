"""Bench core: one RS-485 port under test, driven through the SDK.

The bench keeps a control client for init / deinit / tx and, per
subscription, a dedicated RX client whose stream is drained on a thread: the
SDK ends a subscription only when its client closes. Everything the web
console shows comes from ``snapshot()``.
"""
from __future__ import annotations

import collections
import threading
import time

from . import vectors
from .platform import (MAX_BAUD, MAX_CONSUMER_DELAY_MS, BenchError,
                       PlatformError, StateError, default_client_factory,
                       parse_payload, require_int, require_seconds,
                       sdk_version)
from .runs import MS_IN_NS, S_IN_NS, VectorRun

__all__ = ["Bench", "BenchError", "PlatformError", "StateError",
           "parse_payload", "sdk_version"]

DEFAULT_BAUD = 115200
DEFAULT_QUEUE_SIZE = 16
MAX_QUEUE_SIZE = 128          # device-control caps the backlog here
SHADOW_QUEUE_SIZE = 128       # the second subscriber never drops on purpose
MAX_BURST_COUNT = 100_000
MAX_RTT_COUNT = 10_000
GAP_SAMPLES = 10_000
PONG_CACHE = 256
BURST_ERROR_LIMIT = 10        # consecutive tx failures that abort a burst
BITS_PER_BYTE = 10            # 8N1
SETTINGS = ("baud", "queue_size", "drop_old", "consumer_delay_ms")


def _clock_text(wall_ns):
    secs, ns = divmod(wall_ns, S_IN_NS)
    return (f"{time.strftime('%H:%M:%S', time.localtime(secs))}"
            f".{ns // MS_IN_NS:03d}")


def _printable(frame):
    return "".join(chr(byte) if 32 <= byte < 127 else "." for byte in frame)


def _percentile(sorted_values, fraction):
    if not sorted_values:
        return None
    return sorted_values[min(len(sorted_values) - 1,
                             int(round(fraction * (len(sorted_values) - 1))))]


class Bench:
    """State of the port under test; safe to call from several threads."""

    def __init__(self, client_factory=None, clock=None, sleep=time.sleep,
                 ring_size=500, run_idle_s=2.0, log_frames=False):
        self._factory = client_factory or default_client_factory
        self._clock = clock or time.monotonic_ns
        self._sleep = sleep
        self._ring_size = max(1, int(ring_size))
        self._sdk_version = sdk_version()
        self._lock = threading.RLock()
        self._ctrl = None
        self._clear_epoch = 0
        self.inited = False

        self.baud = DEFAULT_BAUD
        self.config = "8N1"
        self.queue_size = DEFAULT_QUEUE_SIZE
        self.drop_old = True
        self.consumer_delay_ms = 0
        self.log_frames = log_frames
        self.run_idle_s = run_idle_s

        self._rx_thread = None
        self._rx_client = None
        self._stopping = False
        self._stream_started_ns = 0
        self._stream_frames = 0
        self._stream_policy = (self.queue_size, self.drop_old)
        self._pongs = collections.OrderedDict()
        self._rtt_running = False
        self._rtt = {"state": "idle"}
        self._burst_thread = None
        self._burst_stop = threading.Event()
        self._burst = {"state": "idle"}
        self._shadow_client = None
        self._shadow_thread = None
        self._shadow_stopping = False
        self._shadow_error = None
        self._reset_state()

    @property
    def sdk_version(self):
        return self._sdk_version

    # -- settings ----------------------------------------------------------

    def update_settings(self, **changes):
        unknown = sorted(set(changes) - set(SETTINGS))
        if unknown:
            raise ValueError(f"unknown setting(s): {', '.join(unknown)}")
        baud = require_int(changes.get("baud", self.baud), 1, MAX_BAUD,
                           "baud")
        queue_size = require_int(
            changes.get("queue_size", self.queue_size), 0, MAX_QUEUE_SIZE,
            "queue_size")
        drop_old = changes.get("drop_old", self.drop_old)
        if not isinstance(drop_old, bool):
            raise ValueError("drop_old must be a boolean")
        delay = require_int(
            changes.get("consumer_delay_ms", self.consumer_delay_ms), 0,
            MAX_CONSUMER_DELAY_MS, "consumer_delay_ms")
        with self._lock:
            self.baud, self.queue_size = baud, queue_size
            self.drop_old, self.consumer_delay_ms = drop_old, delay

    # -- device-control calls ----------------------------------------------

    def _call(self, what, method):
        with self._lock:
            if self._ctrl is None:
                try:
                    self._ctrl = self._factory()
                except Exception as exc:
                    raise PlatformError(f"{what} failed: {exc}") from exc
            client = self._ctrl
        try:
            return method(client)
        except Exception as exc:
            raise PlatformError(f"{what} failed: {exc}") from exc

    def init(self):
        self._call("init",
                   lambda client: client.rs485_init(self.baud, self.config))
        with self._lock:
            self.inited = True

    def deinit(self):
        self._call("deinit", lambda client: client.rs485_deinit())
        with self._lock:
            self.inited = False

    # -- subscription ------------------------------------------------------

    @property
    def subscribed(self):
        thread = self._rx_thread
        return bool(thread and thread.is_alive() and not self._stopping)

    def start_subscribe(self):
        with self._lock:
            if self.subscribed:
                raise StateError("already subscribed")
            queue_size, drop_old = self.queue_size, self.drop_old
        try:
            client = self._factory()
        except Exception as exc:
            raise PlatformError(f"subscribe failed: {exc}") from exc
        with self._lock:
            self._rx_client = client
            self._stopping = False
            self._stream_started_ns = self._clock()
            self._stream_frames = self.frame_count
            self._stream_policy = (queue_size, drop_old)
            thread = threading.Thread(
                target=self._rx_loop, args=(client, queue_size, drop_old),
                name="rs485-rx", daemon=True)
            self._rx_thread = thread
            thread.start()

    def stop_subscribe(self):
        with self._lock:
            thread, client = self._rx_thread, self._rx_client
            self._stopping = True
        if thread is None:
            return
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _rx_loop(self, client, queue_size, drop_old):
        error = self._pump(client, queue_size, drop_old, self.ingest,
                           lambda: self._stopping,
                           lambda: self.consumer_delay_ms / 1000.0)
        with self._lock:
            if error is None:
                self._last_error = None
            else:
                self._stream_failed = True
                self.stream_errors += 1
                self._last_error = f"{type(error).__name__}: {error}"

    def _pump(self, client, queue_size, drop_old, sink, stopping, delay=None):
        """Drain one subscription into ``sink``; returns the error, if any.

        ``delay`` is asked for a per-frame pause in seconds, which is how the
        slow-consumer mode throttles a subscriber.
        """
        error = None
        try:
            for event in client.subscribe_rs485_rx(queue_size=queue_size,
                                                    drop_old=drop_old):
                if stopping():
                    break
                sink(bytes(event.data), event.timestamp_ns)
                pause = delay() if delay is not None else 0
                if pause:
                    self._sleep(pause)
        except Exception as exc:  # any stream failure ends the loop
            if not stopping():
                error = exc
        finally:
            try:
                client.close()
            except Exception:
                pass
        return error

    def _stream_state_locked(self):
        thread = self._rx_thread
        if thread is None:
            return "idle"
        if not thread.is_alive():
            return "error" if self._stream_failed else "closed"
        if self._stopping:
            return "stopping"
        quiet = self.frame_count == self._stream_frames
        if quiet and self._clock() - self._stream_started_ns < S_IN_NS:
            return "connecting"
        return "active"

    # -- second subscriber -------------------------------------------------

    @property
    def shadow_running(self):
        thread = self._shadow_thread
        return bool(thread and thread.is_alive() and not self._shadow_stopping)

    def start_shadow(self):
        """Open a second, independent subscription that never throttles."""
        with self._lock:
            if self.shadow_running:
                raise StateError("the second subscriber is already running")
        try:
            client = self._factory()
        except Exception as exc:
            raise PlatformError(f"subscribe failed: {exc}") from exc
        with self._lock:
            self._shadow_client = client
            self._shadow_stopping = False
            thread = threading.Thread(
                target=self._shadow_loop, args=(client,),
                name="rs485-rx-shadow", daemon=True)
            self._shadow_thread = thread
            thread.start()

    def stop_shadow(self):
        with self._lock:
            thread, client = self._shadow_thread, self._shadow_client
            self._shadow_stopping = True
        if thread is None:
            return
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        if thread is not threading.current_thread():
            thread.join(timeout=5.0)

    def _shadow_loop(self, client):
        error = self._pump(client, SHADOW_QUEUE_SIZE, True,
                           self._ingest_shadow,
                           lambda: self._shadow_stopping)
        with self._lock:
            self._shadow_error = (
                None if error is None else f"{type(error).__name__}: {error}")

    def _ingest_shadow(self, data, timestamp_ns):
        """Count a frame for the second subscriber and judge its own run."""
        frame = bytes(data)
        t_ns = self._clock()
        with self._lock:
            self.shadow_frames += 1
            self.shadow_bytes += len(frame)
            if not vectors.looks_like_vector(frame):
                return
            try:
                vec = vectors.decode(frame)
            except vectors.VectorError:
                self.shadow_corrupt += 1
                if self._shadow_run.state == "running":
                    self._shadow_run.corrupt += 1
                return
            if vec.kind != vectors.KIND_PING:
                self._shadow_run, _note = self._step(self._shadow_run, vec,
                                                     t_ns)

    def _shadow_state_locked(self):
        thread = self._shadow_thread
        if thread is None:
            return "off"
        if not thread.is_alive():
            return "error" if self._shadow_error else "stopped"
        return "stopping" if self._shadow_stopping else "running"

    # -- receive path ------------------------------------------------------

    def ingest(self, data, timestamp_ns=None, t_ns=None):
        """Account for one received frame (also the hook for offline tests)."""
        frame = bytes(data)
        t_ns = self._clock() if t_ns is None else t_ns
        with self._lock:
            gap_ms = None
            if self._last_arrival_ns is not None:
                gap_ms = (t_ns - self._last_arrival_ns) / MS_IN_NS
                self._gaps.append(gap_ms)
            if self._first_arrival_ns is None:
                self._first_arrival_ns = t_ns
            self._last_arrival_ns = t_ns
            index = self.frame_count
            self.frame_count += 1
            self.byte_count += len(frame)
            record = self._classify_locked(frame, index, t_ns, gap_ms)
            self._ring.append(record)
        if self.log_frames:
            print(f"rx #{index} {record['kind']} len={len(frame)} "
                  f"{frame[:32].hex()}", flush=True)
        return record

    def _classify_locked(self, frame, index, t_ns, gap_ms):
        kind, note, seq = "raw", "", None
        if not vectors.looks_like_vector(frame):
            self.raw_count += 1
        else:
            try:
                vec = vectors.decode(frame)
            except vectors.VectorError as exc:
                kind, note = "corrupt", f"bad {exc.reason}"
                self.corrupt_count += 1
                if self._run.state == "running":
                    self._run.corrupt += 1
            else:
                seq = vec.seq
                if vec.kind == vectors.KIND_PING:
                    kind = "pong"
                    self._remember_pong_locked(seq, t_ns)
                else:
                    previous = self._run
                    self._run, note = self._step(previous, vec, t_ns)
                    self._archive_locked(previous)
                    if vec.kind == vectors.KIND_ANNOUNCE:
                        kind = "announce"
                    else:
                        kind = "vector"
                        self.vector_count += 1
        return {
            "i": index,
            "time": _clock_text(time.time_ns()),
            "gap_ms": None if gap_ms is None else round(gap_ms, 3),
            "len": len(frame),
            "kind": kind,
            "seq": seq,
            "note": note,
            "hex": frame.hex(),
            "text": _printable(frame),
        }

    def _remember_pong_locked(self, seq, t_ns):
        self._pongs[seq] = t_ns
        while len(self._pongs) > PONG_CACHE:
            self._pongs.popitem(last=False)

    # -- numbered runs -----------------------------------------------------

    @staticmethod
    def _step(run, vec, t_ns):
        """Advance ``run`` by one vector; returns (run to keep, frame note)."""
        if vec.kind == vectors.KIND_ANNOUNCE:
            count = vectors.decode_announcement(vec)
            run.finalize(t_ns, "replaced")
            return VectorRun(t_ns, expected=count), f"count {count}"
        if run.state != "running":
            run = VectorRun(t_ns)
        run.add(vec, t_ns)
        if run.complete():
            run.finalize(t_ns, "complete")
            return run, f"seq {vec.seq}, run complete"
        return run, ""

    def _archive_locked(self, run):
        """Keep the verdict of a closed run once, newest last."""
        if run.state == "done" and not run.archived:
            run.archived = True
            self.run_history.append(run.to_dict())

    def _close_idle_runs_locked(self, t_ns):
        """A run with no vector for ``run_idle_s`` is over; judge it."""
        for run in (self._run, self._shadow_run):
            if (run.state == "running"
                    and t_ns - run.last_ns >= self.run_idle_s * S_IN_NS):
                run.finalize(t_ns, "idle timeout")
        self._archive_locked(self._run)

    # -- transmit path -----------------------------------------------------

    def send(self, text=None, hex_=None, encoding="utf-8"):
        data = parse_payload(text=text, hex_=hex_, encoding=encoding)
        self._tx(data, "tx")
        return data

    def _tx(self, data, what):
        try:
            self._call(what, lambda client: client.rs485_tx(data))
        except PlatformError as exc:
            with self._lock:
                self.tx_errors += 1
                self._tx_last_error = str(exc)
            raise
        with self._lock:
            self.tx_frames += 1
            self.tx_bytes += len(data)

    @staticmethod
    def validate_burst(count, size, interval_ms):
        require_int(count, 1, MAX_BURST_COUNT, "count")
        require_int(size, 0, vectors.MAX_PAYLOAD, "size")
        return require_seconds(interval_ms, "interval_ms")

    def start_burst(self, count, size, interval_ms):
        """Send an announcement and ``count`` numbered vectors in the back."""
        interval_s = self.validate_burst(count, size, interval_ms)
        with self._lock:
            if self._burst_thread is not None and self._burst_thread.is_alive():
                raise StateError("a burst is already running")
            self._burst_stop.clear()
            self._burst = {"state": "running", "total": count, "sent": 0,
                           "errors": 0, "size": size,
                           "interval_ms": interval_ms}
            thread = threading.Thread(
                target=self._burst_loop, args=(count, size, interval_s),
                name="rs485-tx-burst", daemon=True)
            self._burst_thread = thread
            thread.start()

    def stop_burst(self):
        self._burst_stop.set()

    def _burst_loop(self, count, size, interval_s):
        burst = self._burst
        failures = 0

        def pause():
            if interval_s:
                self._sleep(interval_s)

        def send(frame, what):
            nonlocal failures
            try:
                self._tx(frame, what)
            except PlatformError:
                failures += 1
                with self._lock:
                    burst["errors"] += 1
                return False
            failures = 0
            return True

        def set_state(state):
            with self._lock:
                burst["state"] = state

        try:
            if not send(vectors.encode_announcement(count),
                        "burst announcement"):
                set_state("error")
                return
            pause()
            for seq in range(1, count + 1):
                if self._burst_stop.is_set():
                    set_state("stopped")
                    return
                payload = vectors.payload_for(seq, size)
                send(vectors.encode(vectors.KIND_DATA, seq, payload), "burst")
                with self._lock:
                    burst["sent"] = seq
                if failures >= BURST_ERROR_LIMIT:
                    set_state("error")
                    return
                pause()
            set_state("done")
        finally:
            with self._lock:
                burst["finished"] = _clock_text(time.time_ns())

    # -- round trip --------------------------------------------------------

    @staticmethod
    def validate_rtt(count, size, interval_ms, timeout_ms):
        require_int(count, 1, MAX_RTT_COUNT, "count")
        require_int(size, 0, vectors.MAX_PAYLOAD, "size")
        interval_s = require_seconds(interval_ms, "interval_ms")
        timeout_s = require_seconds(timeout_ms, "timeout_ms")
        if timeout_s <= 0:
            raise ValueError("timeout_ms must be greater than 0")
        return interval_s, timeout_s

    def check_rtt_ready(self):
        if not self.subscribed:
            raise StateError(
                "Subscribe before running the round-trip test: the echoed "
                "pings arrive on the RX stream")
        if self._rtt_running:
            raise StateError("a round-trip test is already running")

    def run_rtt(self, count, size, interval_ms, timeout_ms):
        """Send pings and time the echoes; needs the peer in echo mode."""
        interval_s, timeout_s = self.validate_rtt(count, size, interval_ms,
                                                   timeout_ms)
        self.check_rtt_ready()
        with self._lock:
            if self._rtt_running:
                raise StateError("a round-trip test is already running")
            self._rtt_running = True
            result = {"state": "running", "count": count, "size": size,
                      "sent": 0, "lost": 0, "samples": [], "min_ms": None,
                      "avg_ms": None, "p50_ms": None, "p99_ms": None,
                      "max_ms": None, "serialization_ms": None,
                      "error": None}
            self._rtt = result
        try:
            self._rtt_loop(result, count, size, interval_s, timeout_s)
        except PlatformError as exc:
            result["error"] = str(exc)
        except Exception as exc:  # report instead of losing the thread
            result["error"] = repr(exc)
        finally:
            self._summarize_rtt(result, size)
            with self._lock:
                self._rtt_running = False
        return dict(result)

    def _rtt_loop(self, result, count, size, interval_s, timeout_s):
        for seq in range(1, count + 1):
            frame = vectors.encode(vectors.KIND_PING, seq,
                                   vectors.payload_for(seq, size))
            with self._lock:
                self._pongs.pop(seq, None)
            started = self._clock()
            self._tx(frame, "ping")
            result["sent"] += 1
            deadline = started + int(timeout_s * S_IN_NS)
            while True:
                with self._lock:
                    arrived = self._pongs.pop(seq, None)
                if arrived is not None or self._clock() >= deadline:
                    break
                self._sleep(min(0.001, timeout_s))
            rtt_ms = None if arrived is None else (arrived - started) / MS_IN_NS
            result["samples"].append({"seq": seq, "rtt_ms": rtt_ms})
            if arrived is None:
                result["lost"] += 1
            if interval_s:
                self._sleep(interval_s)

    def _summarize_rtt(self, result, size):
        values = sorted(sample["rtt_ms"] for sample in result["samples"]
                        if sample["rtt_ms"] is not None)
        result["min_ms"] = values[0] if values else None
        result["avg_ms"] = (sum(values) / len(values)) if values else None
        result["p50_ms"] = _percentile(values, 0.5)
        result["p99_ms"] = _percentile(values, 0.99)
        result["max_ms"] = values[-1] if values else None
        result["serialization_ms"] = (
            (vectors.HEADER.size + vectors.CRC.size + size)
            * BITS_PER_BYTE * 1000.0 / self.baud)
        result["state"] = "done"

    # -- what the console shows --------------------------------------------

    def snapshot(self, since=0):
        """Everything the page shows; ``frames`` holds records from ``since``."""
        with self._lock:
            self._close_idle_runs_locked(self._clock())
            frames = [record for record in self._ring if record["i"] >= since]
            return self._state_locked(frames)

    def report(self):
        """A snapshot without the frame ring, stamped for the record."""
        with self._lock:
            self._close_idle_runs_locked(self._clock())
            state = self._state_locked(None)
        state["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        return state

    def clear(self):
        with self._lock:
            self._reset_state()
            self._clear_epoch += 1

    def close(self):
        self.stop_burst()
        self.stop_subscribe()
        self.stop_shadow()
        thread = self._burst_thread
        if (thread is not None and thread.is_alive()
                and thread is not threading.current_thread()):
            thread.join(timeout=5.0)

    def _reset_state(self):
        self.frame_count = self.byte_count = 0
        self.raw_count = self.vector_count = self.corrupt_count = 0
        self.tx_frames = self.tx_bytes = self.tx_errors = 0
        self._tx_last_error = None
        self.stream_errors = 0
        self._last_error = None
        self._stream_failed = False
        self._ring = collections.deque(maxlen=self._ring_size)
        self._gaps = collections.deque(maxlen=GAP_SAMPLES)
        self._first_arrival_ns = None
        self._last_arrival_ns = None
        self._run = VectorRun.idle()
        self.run_history = collections.deque(maxlen=20)
        self.shadow_frames = self.shadow_bytes = self.shadow_corrupt = 0
        self._shadow_run = VectorRun.idle()

    def _state_locked(self, frames):
        gaps = sorted(self._gaps)
        elapsed_ns = None
        if self._first_arrival_ns is not None and self.frame_count >= 2:
            elapsed_ns = self._last_arrival_ns - self._first_arrival_ns
        state = {
            "sdk_version": self._sdk_version,
            "settings": {
                "baud": self.baud,
                "config": self.config,
                "queue_size": self.queue_size,
                "drop_old": self.drop_old,
                "consumer_delay_ms": self.consumer_delay_ms,
                "run_idle_s": self.run_idle_s,
            },
            "inited": self.inited,
            "stream": {
                "state": self._stream_state_locked(),
                "policy": {
                    "queue_size": self._stream_policy[0],
                    "drop_old": self._stream_policy[1],
                },
                "errors": self.stream_errors,
                "last_error": self._last_error,
            },
            "shadow": {
                "state": self._shadow_state_locked(),
                "queue_size": SHADOW_QUEUE_SIZE,
                "drop_old": True,
                "frames": self.shadow_frames,
                "bytes": self.shadow_bytes,
                "corrupt": self.shadow_corrupt,
                "last_error": self._shadow_error,
                "run": self._shadow_run.to_dict(),
            },
            "counters": {
                "frames": self.frame_count,
                "bytes": self.byte_count,
                "raw": self.raw_count,
                "vectors": self.vector_count,
                "corrupt": self.corrupt_count,
                "elapsed_s": (None if elapsed_ns is None
                              else elapsed_ns / S_IN_NS),
                "fps": (None if not elapsed_ns
                        else (self.frame_count - 1) * S_IN_NS / elapsed_ns),
            },
            "gaps": {
                "count": len(gaps),
                "min_ms": gaps[0] if gaps else None,
                "avg_ms": (sum(gaps) / len(gaps)) if gaps else None,
                "p50_ms": _percentile(gaps, 0.5),
                "p99_ms": _percentile(gaps, 0.99),
                "max_ms": gaps[-1] if gaps else None,
            },
            "run": self._run.to_dict(),
            "run_history": list(self.run_history),
            "tx": {
                "frames": self.tx_frames,
                "bytes": self.tx_bytes,
                "errors": self.tx_errors,
                "last_error": self._tx_last_error,
            },
            "burst": dict(self._burst),
            "rtt": {
                key: (list(value) if key == "samples" else value)
                for key, value in self._rtt.items()
            },
            "clear_epoch": self._clear_epoch,
        }
        if frames is not None:
            state["frames"] = frames
            state["last_index"] = self.frame_count - 1
        return state
