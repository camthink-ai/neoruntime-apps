"""Web layer: routes, validation and error mapping."""

import pytest

from rs485_testbench import bench as bench_mod
from rs485_testbench import vectors as v
from rs485_testbench import web
from conftest import wait_until


@pytest.fixture
def client(rig):
    bench = bench_mod.Bench(client_factory=rig.factory, run_idle_s=0.15)
    app = web.create_app(bench)
    app.config["TESTING"] = True
    with app.test_client() as api:
        yield api, bench
    bench.close()


def post(api, path, body=None):
    return api.post(path, json=body if body is not None else {})


def test_health_reports_the_bench_state(client):
    api, _bench = client

    response = api.get("/api/health")

    assert response.status_code == 200
    body = response.get_json()
    assert body["ok"] is True
    assert body["inited"] is False
    assert body["stream"]["state"] == "idle"


def test_index_page_names_the_tool(client):
    api, _bench = client

    response = api.get("/")

    assert response.status_code == 200
    assert b"RS-485 Testbench" in response.data


def test_status_exposes_every_panel(client):
    api, _bench = client

    body = api.get("/api/status").get_json()

    for key in ("settings", "inited", "stream", "shadow", "counters", "gaps",
                "run", "run_history", "frames", "last_index", "tx", "burst",
                "rtt", "clear_epoch", "sdk_version"):
        assert key in body, key
    assert body["stream"]["state"] == "idle"


def test_status_filters_frames_by_since(client):
    api, bench = client
    for i in range(3):
        bench.ingest(bytes([i]))

    body = api.get("/api/status?since=1").get_json()

    assert [record["i"] for record in body["frames"]] == [1, 2]
    assert body["last_index"] == 2


def test_settings_are_applied_and_validated(client):
    api, bench = client

    assert post(api, "/api/settings",
                {"baud": 9600, "queue_size": 32, "drop_old": False,
                 "consumer_delay_ms": 10}).status_code == 200
    assert (bench.baud, bench.queue_size, bench.drop_old,
            bench.consumer_delay_ms) == (9600, 32, False, 10)

    bad = post(api, "/api/settings", {"baud": 0})
    assert bad.status_code == 400
    assert bad.get_json()["ok"] is False
    assert post(api, "/api/settings", [1, 2]).status_code == 400


def test_init_and_deinit_reach_the_bus(client, rig):
    api, bench = client
    post(api, "/api/settings", {"baud": 19200})

    assert post(api, "/api/init").status_code == 200
    assert bench.inited is True
    assert any(call == ("init", 19200, "8N1")
               for c in rig.clients for call in c.calls)

    assert post(api, "/api/deinit").status_code == 200
    assert bench.inited is False


def test_platform_failures_map_to_502_with_the_message(client, rig):
    api, _bench = client
    rig.fail_next["init"] = RuntimeError("Rs485Init failed: -2815")

    response = post(api, "/api/init")

    assert response.status_code == 502
    assert "-2815" in response.get_json()["error"]


def test_subscribe_lifecycle_and_state_conflict(client, rig):
    api, bench = client

    assert post(api, "/api/subscribe/start").status_code == 200
    assert wait_until(lambda: bench.subscribed)

    conflict = post(api, "/api/subscribe/start")
    assert conflict.status_code == 409

    assert post(api, "/api/subscribe/stop").status_code == 200
    assert wait_until(lambda: not bench.subscribed)


def test_second_subscriber_lifecycle(client):
    api, bench = client
    assert api.get("/api/status").get_json()["shadow"]["state"] == "off"

    assert post(api, "/api/shadow/start").status_code == 200
    assert wait_until(lambda: bench.shadow_running)

    assert post(api, "/api/shadow/start").status_code == 409

    assert post(api, "/api/shadow/stop").status_code == 200
    assert wait_until(lambda: not bench.shadow_running)
    assert api.get("/api/status").get_json()["shadow"]["state"] == "stopped"


def test_tx_accepts_hex_or_text_but_not_both(client, rig):
    api, _bench = client

    assert post(api, "/api/tx", {"hex": "01 02 ff"}).status_code == 200
    assert post(api, "/api/tx", {"text": "ping"}).status_code == 200
    assert rig.tx_calls() == [b"\x01\x02\xff", b"ping"]

    both = post(api, "/api/tx", {"hex": "01", "text": "a"})
    assert both.status_code == 400
    odd = post(api, "/api/tx", {"hex": "abc"})
    assert odd.status_code == 400


def test_burst_start_and_stop(client):
    api, bench = client

    assert post(api, "/api/burst/start",
                {"count": 2, "size": 8, "interval_ms": 0}).status_code == 200
    assert wait_until(lambda: bench.snapshot()["burst"]["state"] == "done")
    assert bench.snapshot()["burst"]["sent"] == 2

    assert post(api, "/api/burst/start",
                {"count": 0, "size": 8, "interval_ms": 0}).status_code == 400
    assert post(api, "/api/burst/stop").status_code == 200


def test_round_trip_runs_in_the_background(client, rig):
    api, bench = client
    post(api, "/api/subscribe/start")
    assert wait_until(lambda: bench.subscribed)

    assert post(api, "/api/rtt/start",
                {"count": 1, "size": 8, "interval_ms": 0,
                 "timeout_ms": 50}).status_code == 200

    assert wait_until(
        lambda: bench.snapshot()["rtt"]["state"] == "done")
    assert bench.snapshot()["rtt"]["lost"] == 1
    assert len(rig.tx_calls()) == 1


def test_round_trip_without_subscription_is_a_state_conflict(client):
    api, _bench = client

    response = post(api, "/api/rtt/start",
                    {"count": 1, "size": 8, "interval_ms": 0,
                     "timeout_ms": 50})

    assert response.status_code == 409


def test_clear_bumps_the_epoch(client):
    api, bench = client
    bench.ingest(b"a")
    before = api.get("/api/status").get_json()["clear_epoch"]

    assert post(api, "/api/clear").status_code == 200

    after = api.get("/api/status").get_json()
    assert after["clear_epoch"] == before + 1
    assert after["counters"]["frames"] == 0


def test_report_downloads_as_a_json_attachment(client):
    api, bench = client
    bench.ingest(v.encode(v.KIND_DATA, 1))

    response = api.get("/api/report")

    assert response.status_code == 200
    assert response.mimetype == "application/json"
    assert "attachment" in response.headers["Content-Disposition"]
    assert "rs485-report" in response.headers["Content-Disposition"]
    assert response.get_json()["counters"]["frames"] == 1


def test_create_app_without_a_bench_builds_a_real_one():
    app = web.create_app()

    assert isinstance(app.config["RS485_BENCH"], bench_mod.Bench)
