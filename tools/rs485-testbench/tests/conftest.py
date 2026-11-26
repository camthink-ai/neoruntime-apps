"""Offline tests for the RS-485 testbench.

Nothing here talks to a device. The bench gets a scripted DeviceClient
stand-in with a controllable RX stream, the web layer gets that bench, and
the PC-side peer script runs against a fake serial port.
"""

import importlib.util
import sys
import threading
import time
import types
from pathlib import Path

import pytest

TOOL_DIR = Path(__file__).resolve().parent.parent
if str(TOOL_DIR) not in sys.path:
    sys.path.insert(0, str(TOOL_DIR))


class Event(types.SimpleNamespace):
    """What DeviceClient.subscribe_rs485_rx() yields."""


class FakeClient:
    """DeviceClient stand-in: records calls, raises scripted failures."""

    def __init__(self, rig):
        self.rig = rig
        self.calls = []
        self.tx_count = 0
        self.connected = False
        self.closed = False
        self.inited = False

    def connect(self):
        self.connected = True

    def close(self):
        self.closed = True
        self.rig.on_close(self)

    def _raise_if_scripted(self, method):
        exc = self.rig.fail_next.pop(method, None)
        if exc is not None:
            raise exc

    def rs485_init(self, baud, config=""):
        self._raise_if_scripted("init")
        self.calls.append(("init", baud, config))
        self.inited = True

    def rs485_deinit(self):
        self._raise_if_scripted("deinit")
        self.calls.append(("deinit",))
        self.inited = False

    def rs485_tx(self, data):
        self._raise_if_scripted("tx")
        exc = self.rig.tx_fail_at.pop(self.tx_count, None)
        self.tx_count += 1
        if exc is not None:
            raise exc
        self.calls.append(("tx", bytes(data)))
        if self.rig.tx_hook is not None:
            self.rig.tx_hook(bytes(data))

    def subscribe_rs485_rx(self, queue_size=16, drop_old=True):
        yield from self.rig.stream(self, queue_size, drop_old)


class Stream:
    """One open subscription with its own bounded backlog (as device-control)."""

    def __init__(self, queue_size, drop_old):
        self.queue_size = min(queue_size or 16, 128)
        self.drop_old = drop_old
        self.pending = []


class Rig:
    """Hands out fake clients and drives the RX stream of every subscriber."""

    def __init__(self):
        self.clients = []
        self.streams = []
        self.stream_calls = []
        self.stream_opened = threading.Event()
        self.stream_ended = threading.Event()
        self.end_with = None
        self.tx_hook = None
        self.fail_next = {}
        self.tx_fail_at = {}
        self._cv = threading.Condition()

    def factory(self):
        client = FakeClient(self)
        self.clients.append(client)
        return client

    def push(self, data, timestamp_ns=None):
        """Deliver one frame to every open subscription."""
        event = Event(data=bytes(data),
                      timestamp_ns=timestamp_ns or time.time_ns())
        with self._cv:
            for stream in self.streams:
                if len(stream.pending) >= stream.queue_size:
                    if not stream.drop_old:
                        continue            # the newest frame is dropped
                    stream.pending.pop(0)   # the oldest frame is dropped
                stream.pending.append(event)
            self._cv.notify_all()

    def stream(self, client, queue_size, drop_old):
        stream = Stream(queue_size, drop_old)
        self.stream_calls.append((queue_size, drop_old))
        self.streams.append(stream)
        self.stream_opened.set()
        try:
            while True:
                with self._cv:
                    while (not stream.pending and not client.closed
                           and self.end_with is None):
                        self._cv.wait(timeout=0.02)
                    if client.closed or self.end_with is not None:
                        break
                    event = stream.pending.pop(0)
                yield event
            if self.end_with is not None:
                raise self.end_with
        finally:
            self.streams.remove(stream)
            self.stream_ended.set()

    def on_close(self, client):
        with self._cv:
            self._cv.notify_all()

    def tx_calls(self):
        """Bytes passed to rs485_tx, by every client, in call order."""
        return [call[1] for client in self.clients
                for call in client.calls if call[0] == "tx"]


def wait_until(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


@pytest.fixture
def rig():
    return Rig()


@pytest.fixture(scope="session")
def peer_mod():
    """Load peer/rs485_peer.py: a single file that runs without the app."""
    spec = importlib.util.spec_from_file_location(
        "rs485_peer", TOOL_DIR / "peer" / "rs485_peer.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["rs485_peer"] = module
    spec.loader.exec_module(module)
    return module
