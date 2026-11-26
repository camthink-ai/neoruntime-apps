"""platform.py: the SDK-facing helpers that need no device."""

import importlib.metadata
import sys
import types

import pytest

from rs485_testbench import platform


def test_sdk_version_is_none_without_the_package(monkeypatch):
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(platform.importlib.metadata, "version", missing)

    assert platform.sdk_version() is None


def test_sdk_version_reports_the_installed_release(monkeypatch):
    monkeypatch.setattr(platform.importlib.metadata, "version",
                        lambda name: "0.8.2")

    assert platform.sdk_version() == "0.8.2"


def test_default_client_factory_connects_before_handing_out_the_client(
        monkeypatch):
    made = []

    class Client:
        def __init__(self):
            made.append(self)
            self.connected = False

        def connect(self):
            self.connected = True

    sdk = types.ModuleType("neoruntime_ipc_sdk")
    sdk.DeviceClient = Client
    monkeypatch.setitem(sys.modules, "neoruntime_ipc_sdk", sdk)

    client = platform.default_client_factory()

    assert made == [client]
    assert client.connected is True


@pytest.mark.parametrize("bad", [True, 2.5, "5", 0, 11, None])
def test_require_int_rejects_anything_but_an_int_in_range(bad):
    with pytest.raises(ValueError, match="n must be an integer"):
        platform.require_int(bad, 1, 10, "n")


def test_require_int_passes_an_in_range_int_through():
    assert platform.require_int(5, 1, 10, "n") == 5


def test_require_seconds_accepts_int_and_float():
    assert platform.require_seconds(250, "t") == 0.25
    assert platform.require_seconds(0, "t") == 0.0
    for bad in (True, -1, "1", None):
        with pytest.raises(ValueError):
            platform.require_seconds(bad, "t")


def test_error_hierarchy():
    assert issubclass(platform.StateError, platform.BenchError)
    assert issubclass(platform.PlatformError, platform.BenchError)
