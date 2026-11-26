"""Process entry: a real Bench behind the web console."""
from __future__ import annotations

import logging
import os
import signal
import sys

from . import __version__
from .bench import Bench
from .web import create_app

LOG = logging.getLogger("rs485_testbench")
DEFAULT_PORT = 8097


def _port_from_env():
    try:
        port = int(os.environ.get("WEB_PORT", DEFAULT_PORT))
    except ValueError:
        return DEFAULT_PORT
    return port if 1 <= port <= 65535 else DEFAULT_PORT


class BenchApplication:
    def __init__(self, port=None):
        self.port = _port_from_env() if port is None else port
        self.bench = Bench(
            log_frames=os.environ.get("LOG_FRAMES") == "1")
        self.flask_app = create_app(self.bench)

    def run(self):
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        LOG.info("RS-485 Testbench %s, neoruntime-ipc-sdk %s, serving on "
                 "0.0.0.0:%d", __version__,
                 self.bench.sdk_version or "not installed", self.port)
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        from waitress import serve

        serve(self.flask_app, host="0.0.0.0", port=self.port, threads=4)

    def _on_signal(self, signum, _frame):
        LOG.info("signal %d: shutting down", signum)
        try:
            self.bench.close()
        except Exception:  # the process is leaving either way
            LOG.exception("bench shutdown failed")
        sys.exit(0)


def main():
    BenchApplication().run()
    return 0
