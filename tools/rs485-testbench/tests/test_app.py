"""Process entry: port resolution, wiring and the signal shutdown."""

import runpy
import sys
import types

import pytest

from rs485_testbench import app as app_mod


def test_port_comes_from_the_environment_with_sane_fallbacks(monkeypatch):
    monkeypatch.delenv("WEB_PORT", raising=False)
    assert app_mod._port_from_env() == app_mod.DEFAULT_PORT

    monkeypatch.setenv("WEB_PORT", "9000")
    assert app_mod._port_from_env() == 9000

    for broken in ("not-a-port", "0", "70000"):
        monkeypatch.setenv("WEB_PORT", broken)
        assert app_mod._port_from_env() == app_mod.DEFAULT_PORT


def test_application_wires_a_bench_into_the_flask_app():
    application = app_mod.BenchApplication(port=8097)

    assert application.port == 8097
    assert application.flask_app.config["RS485_BENCH"] is application.bench
    assert application.bench.log_frames is False


def test_log_frames_follows_the_environment(monkeypatch):
    monkeypatch.setenv("LOG_FRAMES", "1")

    assert app_mod.BenchApplication(port=8097).bench.log_frames is True


def test_signal_closes_the_bench_and_exits_cleanly():
    application = app_mod.BenchApplication(port=8097)
    closed = []
    application.bench.close = lambda: closed.append(1)

    with pytest.raises(SystemExit) as stop:
        application._on_signal(15, None)

    assert stop.value.code == 0
    assert closed == [1]


def test_signal_exit_survives_a_failing_bench(caplog):
    application = app_mod.BenchApplication(port=8097)

    def blow_up():
        raise RuntimeError("already gone")

    application.bench.close = blow_up

    with pytest.raises(SystemExit) as stop:
        application._on_signal(15, None)

    assert stop.value.code == 0
    assert "bench shutdown failed" in caplog.text


def test_main_serves_the_console_on_all_interfaces(monkeypatch):
    served = {}

    def serve(flask_app, host, port, threads):
        served.update(host=host, port=port, threads=threads, app=flask_app)

    waitress = types.ModuleType("waitress")
    waitress.serve = serve
    monkeypatch.setitem(sys.modules, "waitress", waitress)
    monkeypatch.setenv("WEB_PORT", "8097")

    assert app_mod.main() == 0

    assert served["host"] == "0.0.0.0"
    assert served["port"] == 8097
    assert served["threads"] == 4
    assert served["app"].config["RS485_BENCH"] is not None


def test_dunder_main_delegates_to_main(monkeypatch):
    monkeypatch.setattr(app_mod, "main", lambda: 3)

    with pytest.raises(SystemExit) as stop:
        runpy.run_module("rs485_testbench", run_name="__main__")

    assert stop.value.code == 3
