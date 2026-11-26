"""Flask routes: a thin JSON surface over Bench plus the console page."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from flask import Flask, Response, jsonify, render_template, request

from .bench import Bench, BenchError, PlatformError, StateError

# Sibling of the package both in the repository (tools/rs485-testbench) and
# in the image (/app).
TEMPLATES = Path(__file__).resolve().parent.parent / "templates"


def _body():
    payload = request.get_json(silent=True)
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise ValueError("the request body must be a JSON object")
    return payload


def _int(body, name, default):
    value = body.get(name, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def create_app(bench=None):
    bench = bench if bench is not None else Bench()
    app = Flask(__name__, template_folder=str(TEMPLATES))
    app.config["RS485_BENCH"] = bench

    @app.errorhandler(ValueError)
    def _invalid(exc):
        return jsonify(ok=False, error=str(exc)), 400

    @app.errorhandler(StateError)
    def _conflict(exc):
        return jsonify(ok=False, error=str(exc)), 409

    @app.errorhandler(PlatformError)
    def _platform(exc):
        return jsonify(ok=False, error=str(exc)), 502

    @app.errorhandler(BenchError)
    def _bench_error(exc):
        return jsonify(ok=False, error=str(exc)), 500

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        stream = bench.snapshot()["stream"]
        return jsonify(ok=True, inited=bench.inited,
                       stream={"state": stream["state"]})

    @app.get("/api/status")
    def status():
        return jsonify(bench.snapshot(
            since=request.args.get("since", 0, type=int)))

    @app.post("/api/settings")
    def settings():
        bench.update_settings(**_body())
        return jsonify(ok=True)

    @app.post("/api/init")
    def init():
        bench.init()
        return jsonify(ok=True)

    @app.post("/api/deinit")
    def deinit():
        bench.deinit()
        return jsonify(ok=True)

    @app.post("/api/subscribe/start")
    def subscribe_start():
        bench.start_subscribe()
        return jsonify(ok=True)

    @app.post("/api/subscribe/stop")
    def subscribe_stop():
        bench.stop_subscribe()
        return jsonify(ok=True)

    @app.post("/api/shadow/start")
    def shadow_start():
        bench.start_shadow()
        return jsonify(ok=True)

    @app.post("/api/shadow/stop")
    def shadow_stop():
        bench.stop_shadow()
        return jsonify(ok=True)

    @app.post("/api/tx")
    def tx():
        body = _body()
        data = bench.send(text=body.get("text"), hex_=body.get("hex"),
                          encoding=body.get("encoding", "utf-8"))
        return jsonify(ok=True, sent=len(data))

    @app.post("/api/burst/start")
    def burst_start():
        body = _body()
        bench.start_burst(_int(body, "count", 100),
                          _int(body, "size", 32),
                          _int(body, "interval_ms", 20))
        return jsonify(ok=True)

    @app.post("/api/burst/stop")
    def burst_stop():
        bench.stop_burst()
        return jsonify(ok=True)

    @app.post("/api/rtt/start")
    def rtt_start():
        body = _body()
        count = _int(body, "count", 20)
        size = _int(body, "size", 16)
        interval_ms = _int(body, "interval_ms", 100)
        timeout_ms = _int(body, "timeout_ms", 1000)
        bench.validate_rtt(count, size, interval_ms, timeout_ms)
        bench.check_rtt_ready()
        threading.Thread(
            target=bench.run_rtt,
            args=(count, size, interval_ms, timeout_ms),
            name="rs485-rtt", daemon=True).start()
        return jsonify(ok=True)

    @app.post("/api/clear")
    def clear():
        bench.clear()
        return jsonify(ok=True)

    @app.get("/api/report")
    def report():
        stamp = time.strftime("%Y%m%d-%H%M%S")
        body = json.dumps(bench.report(), indent=2, ensure_ascii=False)
        return Response(
            body, mimetype="application/json",
            headers={"Content-Disposition":
                     f"attachment; filename=rs485-report-{stamp}.json"})

    return app
