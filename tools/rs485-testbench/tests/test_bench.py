"""Bench core: counters, run verdicts, TX burst and the round-trip test."""

import threading
import time

import pytest

from rs485_testbench import bench as bench_mod
from rs485_testbench import vectors as v
from conftest import wait_until

DATA = v.KIND_DATA


def make_bench(rig, **kwargs):
    kwargs.setdefault("run_idle_s", 0.15)
    return bench_mod.Bench(client_factory=rig.factory, **kwargs)


@pytest.fixture
def bench(rig):
    bench = make_bench(rig)
    yield bench
    bench.close()


def start(bench, rig):
    bench.start_subscribe()
    assert rig.stream_opened.wait(2.0)
    assert wait_until(lambda: rig.stream_calls)


def receive(bench, rig, *frames):
    """Push frames through the live subscription and wait until ingested.

    Waits on the plain counter on purpose: snapshot() closes an idle run, and
    the verdict tests set run_idle_s to 0.
    """
    before = bench.frame_count
    for frame in frames:
        rig.push(frame)
    total = before + len(frames)
    assert wait_until(lambda: bench.frame_count == total)
    return total


def data(seq, size=8):
    return v.encode(DATA, seq, v.payload_for(seq, size))


# --- init / deinit ---------------------------------------------------------


def test_init_and_deinit_drive_the_control_client(rig):
    bench = make_bench(rig)
    bench.baud = 9600

    bench.init()

    assert bench.inited is True
    assert any(call == ("init", 9600, "8N1")
               for client in rig.clients for call in client.calls)
    bench.deinit()
    assert bench.inited is False


def test_init_failure_is_wrapped_and_leaves_the_bus_uninited(rig):
    bench = make_bench(rig)
    rig.fail_next["init"] = RuntimeError("Rs485Init failed: -2815")

    with pytest.raises(bench_mod.PlatformError, match="init"):
        bench.init()

    assert bench.inited is False


# --- subscription ----------------------------------------------------------


def test_frames_arrive_as_events_with_counters_and_a_ring(bench, rig):
    start(bench, rig)

    receive(bench, rig, b"hello", b"\x00\xff\x10", v.encode(DATA, 1, b"\x01"))

    snap = bench.snapshot()
    assert snap["counters"]["frames"] == 3
    assert snap["counters"]["bytes"] == 5 + 3 + 14
    assert snap["counters"]["raw"] == 2
    assert snap["counters"]["vectors"] == 1
    kinds = [record["kind"] for record in snap["frames"]]
    assert kinds == ["raw", "raw", "vector"]
    assert snap["frames"][0]["hex"] == "68656c6c6f"
    assert snap["frames"][0]["text"] == "hello"
    assert snap["frames"][1]["text"] == "..."
    assert snap["frames"][2]["seq"] == 1
    assert bench.subscribed is True


def test_queue_policy_is_passed_to_the_sdk(bench, rig):
    bench.update_settings(queue_size=128, drop_old=False)

    start(bench, rig)

    assert rig.stream_calls == [(128, False)]


def test_stop_subscribe_closes_the_rx_client_and_joins_the_thread(bench, rig):
    start(bench, rig)

    bench.stop_subscribe()

    assert rig.clients[0].closed is True
    assert rig.stream_ended.wait(2.0)
    assert bench.subscribed is False


def test_stream_failure_is_recorded_when_not_stopping(bench, rig):
    start(bench, rig)

    rig.end_with = RuntimeError("UNIMPLEMENTED")

    assert wait_until(lambda: not bench.subscribed)
    stream = bench.snapshot()["stream"]
    assert stream["errors"] == 1
    assert "UNIMPLEMENTED" in stream["last_error"]
    assert stream["state"] == "error"


def test_starting_twice_is_a_state_error(bench, rig):
    start(bench, rig)

    with pytest.raises(bench_mod.StateError, match="already"):
        bench.start_subscribe()


def test_consumer_delay_slows_the_rx_loop_between_frames(rig):
    delays = []
    bench = make_bench(rig, sleep=delays.append)
    bench.consumer_delay_ms = 30

    start(bench, rig)
    receive(bench, rig, b"a", b"b")

    assert wait_until(lambda: len(delays) >= 2)
    assert delays[:2] == [0.03, 0.03]


def test_ring_buffer_keeps_only_the_newest_records(rig):
    bench = make_bench(rig, ring_size=5)

    for i in range(8):
        bench.ingest(bytes([i]))

    snap = bench.snapshot()
    assert snap["counters"]["frames"] == 8
    assert [record["i"] for record in snap["frames"]] == [3, 4, 5, 6, 7]
    assert snap["last_index"] == 7


def test_snapshot_since_returns_only_newer_records(bench, rig):
    for i in range(4):
        bench.ingest(bytes([i]))

    snap = bench.snapshot(since=2)

    assert [record["i"] for record in snap["frames"]] == [2, 3]


def test_gap_statistics_use_arrival_times(rig):
    now = [0]
    bench = make_bench(rig, clock=lambda: now[0])

    for i, tick in enumerate((0, 5_000_000, 50_000_000)):
        now[0] = tick
        bench.ingest(bytes([i]))

    gaps = bench.snapshot()["gaps"]
    assert gaps["count"] == 2
    assert gaps["min_ms"] == 5.0
    assert gaps["max_ms"] == 45.0
    assert gaps["avg_ms"] == 25.0


# --- settings --------------------------------------------------------------


def test_settings_are_validated(bench):
    bench.update_settings(baud=9600, queue_size=64, drop_old=False,
                          consumer_delay_ms=25)
    assert (bench.baud, bench.queue_size, bench.drop_old,
            bench.consumer_delay_ms) == (9600, 64, False, 25)

    for change in ({"baud": 0}, {"baud": "9600"}, {"queue_size": 200},
                   {"consumer_delay_ms": -1}, {"nope": 1}):
        with pytest.raises(ValueError):
            bench.update_settings(**change)


# --- run verdicts ----------------------------------------------------------


def test_run_passes_when_every_vector_arrives_in_order(bench, rig):
    bench.run_idle_s = 0
    start(bench, rig)

    receive(bench, rig, v.encode_announcement(3), data(1), data(2), data(3))

    run = bench.snapshot()["run"]
    assert run["state"] == "done"
    assert run["verdict"] == "pass"
    assert run["expected"] == 3
    assert run["ok"] == 3
    assert run["missing"] == []


def test_missing_sequence_numbers_fail_the_run_after_the_idle_timeout(
        bench, rig):
    bench.run_idle_s = 0
    start(bench, rig)

    receive(bench, rig, v.encode_announcement(5), data(1), data(2), data(4),
            data(5))

    run = bench.snapshot()["run"]
    assert run["state"] == "done"
    assert run["verdict"] == "fail"
    assert run["missing"] == [3]
    assert run["head_kept"] is True
    assert run["tail_kept"] is True


def test_dropped_oldest_frames_are_visible_as_a_missing_head(bench, rig):
    bench.run_idle_s = 0
    start(bench, rig)

    receive(bench, rig, v.encode_announcement(10),
            *(data(s) for s in range(5, 11)))

    run = bench.snapshot()["run"]
    assert run["verdict"] == "fail"
    assert run["missing"] == [1, 2, 3, 4]
    assert run["head_kept"] is False
    assert run["tail_kept"] is True


def test_duplicates_and_out_of_order_arrivals_fail_the_run(bench, rig):
    bench.run_idle_s = 0
    start(bench, rig)

    receive(bench, rig, v.encode_announcement(3), data(2), data(1), data(2),
            data(3))

    run = bench.snapshot()["run"]
    assert run["verdict"] == "fail"
    assert run["ok"] == 3
    assert run["dup"] == 1
    assert run["out_of_order"] == 1
    assert run["missing"] == []


def test_corrupt_vector_is_counted_in_the_active_run(bench, rig):
    bench.run_idle_s = 0
    start(bench, rig)
    broken = bytearray(data(1))
    broken[-1] ^= 0xFF

    receive(bench, rig, v.encode_announcement(1), bytes(broken))

    snap = bench.snapshot()
    assert snap["counters"]["corrupt"] == 1
    assert snap["frames"][-1]["kind"] == "corrupt"
    assert snap["run"]["corrupt"] == 1
    assert snap["run"]["verdict"] == "fail"


def test_payload_that_breaks_the_pattern_fails_the_run(bench, rig):
    bench.run_idle_s = 0
    start(bench, rig)

    receive(bench, rig, v.encode_announcement(1),
            v.encode(DATA, 1, b"\x00" * 8))

    run = bench.snapshot()["run"]
    assert run["payload_bad"] == 1
    assert run["verdict"] == "fail"


def test_data_without_announcement_gets_an_implicit_run(bench, rig):
    bench.run_idle_s = 0
    start(bench, rig)

    receive(bench, rig, data(1), data(3), data(2))

    run = bench.snapshot()["run"]
    assert run["expected"] is None
    assert run["missing"] == []
    assert run["verdict"] == "fail"


def test_implicit_run_reports_gaps_in_the_observed_range(bench, rig):
    bench.run_idle_s = 0
    start(bench, rig)

    receive(bench, rig, data(4), data(6))

    run = bench.snapshot()["run"]
    assert run["missing"] == [5]
    assert run["verdict"] == "fail"


def test_a_new_announcement_closes_the_previous_run(bench, rig):
    bench.run_idle_s = 0
    start(bench, rig)

    receive(bench, rig, v.encode_announcement(5), data(1),
            v.encode_announcement(3), data(1), data(2), data(3))

    snap = bench.snapshot()
    assert snap["run"]["expected"] == 3
    assert snap["run"]["verdict"] == "pass"
    assert len(snap["run_history"]) == 2
    previous = snap["run_history"][0]
    assert previous["expected"] == 5
    assert previous["verdict"] == "fail"
    assert previous["reason"] == "replaced"
    assert previous["missing"] == [2, 3, 4, 5]


def test_implicit_run_passes_when_vectors_arrive_in_order(bench, rig):
    bench.run_idle_s = 0
    start(bench, rig)

    receive(bench, rig, data(1), data(2), data(3))

    run = bench.snapshot()["run"]
    assert run["expected"] is None
    assert run["verdict"] == "pass"


# --- clear / report --------------------------------------------------------


def test_clear_resets_counters_and_bumps_the_epoch(bench, rig):
    bench.ingest(data(1))
    before = bench.snapshot()

    bench.clear()

    after = bench.snapshot()
    assert after["clear_epoch"] == before["clear_epoch"] + 1
    assert after["counters"]["frames"] == 0
    assert after["frames"] == []
    assert after["run"]["state"] == "idle"


def test_report_is_a_snapshot_without_the_frame_ring(bench):
    bench.ingest(data(1))

    report = bench.report()

    assert "frames" not in report
    assert report["counters"]["frames"] == 1
    assert "generated_at" in report
    assert report["sdk_version"] is None or isinstance(
        report["sdk_version"], str)


# --- TX --------------------------------------------------------------------


def test_send_encodes_hex_and_text(bench, rig):
    assert bench.send(hex_="01 02, ff:0A") == b"\x01\x02\xff\x0a"
    assert bench.send(text="héllo", encoding="utf-8") == "héllo".encode()

    assert rig.tx_calls() == [b"\x01\x02\xff\x0a", "héllo".encode()]
    snap = bench.snapshot()
    assert snap["tx"]["frames"] == 2
    assert snap["tx"]["bytes"] == 4 + len("héllo".encode())


def test_send_rejects_ambiguous_or_malformed_input(bench):
    with pytest.raises(ValueError, match="hex"):
        bench.send(hex_="abc")
    with pytest.raises(ValueError, match="hex"):
        bench.send(hex_="zz")
    with pytest.raises(ValueError, match="one"):
        bench.send(text="a", hex_="01")
    with pytest.raises(ValueError):
        bench.send()
    with pytest.raises(ValueError, match="512"):
        bench.send(text="x" * 513)


def test_tx_failure_is_wrapped_and_counted(bench, rig):
    rig.fail_next["tx"] = RuntimeError("Rs485Tx failed")

    with pytest.raises(bench_mod.PlatformError, match="tx"):
        bench.send(text="a")

    tx = bench.snapshot()["tx"]
    assert tx["errors"] == 1
    assert "Rs485Tx failed" in tx["last_error"]


def test_burst_sends_an_announcement_then_the_numbered_vectors(rig):
    bench = make_bench(rig)

    bench.start_burst(count=3, size=8, interval_ms=0)

    assert wait_until(lambda: bench.snapshot()["burst"]["state"] == "done")
    sent = rig.tx_calls()
    assert len(sent) == 4
    assert v.decode_announcement(v.decode(sent[0])) == 3
    decoded = [v.decode(frame) for frame in sent[1:]]
    assert [vec.seq for vec in decoded] == [1, 2, 3]
    assert all(vec.kind == DATA and vec.payload == v.payload_for(vec.seq, 8)
               for vec in decoded)
    burst = bench.snapshot()["burst"]
    assert burst["sent"] == 3
    assert burst["errors"] == 0


def test_burst_counts_tx_errors_and_still_finishes(rig):
    bench = make_bench(rig)
    rig.tx_fail_at = {2: RuntimeError("boom")}

    bench.start_burst(count=3, size=4, interval_ms=0)

    assert wait_until(lambda: bench.snapshot()["burst"]["state"] == "done")
    burst = bench.snapshot()["burst"]
    assert burst["sent"] == 3
    assert burst["errors"] == 1
    assert len(rig.tx_calls()) == 3


def test_stop_burst_ends_a_long_burst_early(rig):
    bench = make_bench(rig)

    bench.start_burst(count=2000, size=16, interval_ms=2)
    assert wait_until(lambda: bench.snapshot()["burst"]["sent"] >= 5)
    bench.stop_burst()

    assert wait_until(lambda: bench.snapshot()["burst"]["state"] == "stopped")
    assert bench.snapshot()["burst"]["sent"] < 2000


def test_burst_rejects_out_of_range_arguments(bench):
    for kwargs in ({"count": 0, "size": 8, "interval_ms": 10},
                   {"count": 10, "size": v.MAX_PAYLOAD + 1, "interval_ms": 10},
                   {"count": 10, "size": 8, "interval_ms": -1}):
        with pytest.raises(ValueError):
            bench.start_burst(**kwargs)


# --- round trip ------------------------------------------------------------


def test_round_trip_measures_each_echoed_ping(bench, rig):
    start(bench, rig)
    rig.tx_hook = lambda frame: threading.Timer(
        0.005, rig.push, args=(frame,)).start()

    result = bench.run_rtt(count=3, size=8, interval_ms=0, timeout_ms=1000)

    assert result["state"] == "done"
    assert result["sent"] == 3
    assert result["lost"] == 0
    assert len(result["samples"]) == 3
    assert all(sample["rtt_ms"] >= 4.0 for sample in result["samples"])
    assert result["min_ms"] >= 4.0
    assert result["max_ms"] >= result["min_ms"]
    assert result["serialization_ms"] > 0
    assert bench.snapshot()["rtt"]["sent"] == 3


def test_round_trip_counts_unanswered_pings_as_lost(bench, rig):
    start(bench, rig)

    result = bench.run_rtt(count=2, size=8, interval_ms=0, timeout_ms=30)

    assert result["lost"] == 2
    assert [sample["rtt_ms"] for sample in result["samples"]] == [None, None]


def test_round_trip_needs_a_running_subscription(bench):
    with pytest.raises(bench_mod.StateError, match="Subscribe"):
        bench.run_rtt(count=1, size=8, interval_ms=0, timeout_ms=100)


def test_round_trip_rejects_bad_arguments(bench, rig):
    start(bench, rig)

    with pytest.raises(ValueError):
        bench.run_rtt(count=0, size=8, interval_ms=0, timeout_ms=100)
    with pytest.raises(ValueError):
        bench.run_rtt(count=1, size=8, interval_ms=0, timeout_ms=0)


# --- helpers ---------------------------------------------------------------


@pytest.mark.parametrize("text, encoding, expected", [
    ("abc", "utf-8", b"abc"),
    ("héllo", "utf-8", "héllo".encode()),
    ("héllo", "latin-1", "héllo".encode("latin-1")),
])
def test_parse_payload_text(text, encoding, expected):
    assert bench_mod.parse_payload(text=text, encoding=encoding) == expected


def test_parse_payload_hex_ignores_separators():
    assert bench_mod.parse_payload(
        hex_=" 01 02,ff:0a\n03 ") == b"\x01\x02\xff\x0a\x03"


def test_parse_payload_rejects_unknown_encoding():
    with pytest.raises(ValueError, match="encoding"):
        bench_mod.parse_payload(text="abc", encoding="nope")


# --- failure paths ---------------------------------------------------------


def test_control_client_creation_failure_is_a_platform_error():
    def factory():
        raise OSError("device-control socket missing")

    bench = bench_mod.Bench(client_factory=factory)

    with pytest.raises(bench_mod.PlatformError, match="init failed"):
        bench.init()

    assert bench.inited is False


def test_rx_client_creation_failure_is_a_platform_error(rig):
    made = []

    def factory():
        made.append(1)
        if made:
            raise OSError("no channel left")
        return rig.factory()

    bench = make_bench(rig)
    bench._factory = factory

    with pytest.raises(bench_mod.PlatformError, match="subscribe failed"):
        bench.start_subscribe()

    assert bench.subscribed is False


def test_log_frames_prints_one_line_per_frame(rig, capsys):
    bench = make_bench(rig, log_frames=True)

    bench.ingest(b"hi")

    assert "rx #0 raw len=2 6869" in capsys.readouterr().out


def test_close_stops_the_burst_and_the_subscription(bench, rig):
    start(bench, rig)
    bench.start_burst(count=5000, size=8, interval_ms=2)

    bench.close()

    assert bench.subscribed is False
    assert bench.snapshot()["burst"]["state"] == "stopped"


def test_round_trip_reports_a_tx_failure(bench, rig):
    start(bench, rig)
    rig.fail_next["tx"] = RuntimeError("Rs485Tx failed")

    result = bench.run_rtt(count=1, size=8, interval_ms=0, timeout_ms=50)

    assert result["state"] == "done"
    assert "Rs485Tx failed" in result["error"]
    assert bench.snapshot()["rtt"]["error"] == result["error"]


def test_burst_gives_up_after_ten_tx_failures_in_a_row(rig):
    bench = make_bench(rig)
    bench._factory = lambda: _FailingClient()
    with pytest.raises(bench_mod.PlatformError):
        bench.send(text="x")

    bench.start_burst(count=50, size=4, interval_ms=0)

    assert wait_until(
        lambda: bench.snapshot()["burst"]["state"] not in ("idle", "running"))
    assert bench.snapshot()["burst"]["state"] == "error"


class _FailingClient:
    """Every call fails, as when device-control is away."""

    def connect(self):
        pass

    def close(self):
        pass

    def rs485_tx(self, _data):
        raise RuntimeError("transport closed")


# --- second subscriber -----------------------------------------------------


def start_shadow(bench, rig):
    opened = len(rig.stream_calls)
    bench.start_shadow()
    assert wait_until(lambda: len(rig.stream_calls) == opened + 1)


def test_second_subscriber_gets_every_frame_on_its_own_stream(bench, rig):
    start(bench, rig)
    start_shadow(bench, rig)

    receive(bench, rig, v.encode_announcement(3), data(1), data(2), data(3))
    assert wait_until(lambda: bench.shadow_frames == 4)

    snap = bench.snapshot()
    assert rig.stream_calls == [(16, True), (128, True)]
    assert snap["shadow"]["state"] == "running"
    assert snap["shadow"]["run"]["verdict"] == "pass"
    assert snap["shadow"]["run"]["ok"] == 3
    assert snap["run"]["verdict"] == "pass"


def test_second_subscriber_keeps_receiving_after_the_main_stream_stops(
        bench, rig):
    start(bench, rig)
    start_shadow(bench, rig)

    bench.stop_subscribe()
    rig.push(data(1))
    assert wait_until(lambda: bench.shadow_frames == 1)

    assert bench.shadow_running is True
    assert bench.subscribed is False


def test_slow_main_subscriber_drops_while_the_second_keeps_everything(rig):
    bench = make_bench(rig)
    bench.update_settings(queue_size=16, consumer_delay_ms=20)
    bench.run_idle_s = 0
    start(bench, rig)
    start_shadow(bench, rig)
    burst = [v.encode_announcement(60)] + [data(seq, 4) for seq in range(1, 61)]

    for frame in burst:            # the peer talks faster than main consumes
        rig.push(frame)
        time.sleep(0.002)

    assert wait_until(lambda: bench.shadow_frames == 61)
    settle(bench)
    snap = bench.snapshot()
    assert snap["shadow"]["run"]["verdict"] == "pass"
    assert snap["shadow"]["run"]["ok"] == 60
    assert snap["run"]["verdict"] == "fail"
    assert snap["run"]["missing_count"] > 0
    assert snap["run"]["head_kept"] is True
    assert snap["run"]["tail_kept"] is True


def settle(bench):
    """Wait until the main stream has drained its backlog."""
    last = -1
    while True:
        time.sleep(0.3)
        if bench.frame_count == last:
            return last
        last = bench.frame_count


def test_second_subscriber_cannot_start_twice(bench, rig):
    start_shadow(bench, rig)

    with pytest.raises(bench_mod.StateError, match="already running"):
        bench.start_shadow()


def test_second_subscriber_failure_is_recorded(bench, rig):
    start_shadow(bench, rig)

    rig.end_with = RuntimeError("UNIMPLEMENTED")

    assert wait_until(lambda: not bench.shadow_running)
    shadow = bench.snapshot()["shadow"]
    assert shadow["state"] == "error"
    assert "UNIMPLEMENTED" in shadow["last_error"]


def test_stopping_the_second_subscriber_closes_its_client(bench, rig):
    start_shadow(bench, rig)

    bench.stop_shadow()

    assert rig.clients[0].closed is True
    assert bench.shadow_running is False
    assert bench.snapshot()["shadow"]["state"] == "stopped"
