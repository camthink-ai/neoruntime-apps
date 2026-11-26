"""Talking to device-control: errors, payload parsing, argument checks."""
from __future__ import annotations

import importlib.metadata

from . import vectors

MAX_BAUD = 4_000_000
MAX_CONSUMER_DELAY_MS = 10_000
MAX_TX_PAYLOAD = vectors.MAX_FRAME
_HEX_CLEANUP = str.maketrans({mark: None for mark in " ,:\t\r\n-_"})
_HEXDIGITS = frozenset("0123456789abcdefABCDEF")


class BenchError(Exception):
    """Base class for the errors the web layer maps onto HTTP statuses."""


class StateError(BenchError):
    """The request conflicts with what the bench is doing right now."""


class PlatformError(BenchError):
    """A device-control call failed; the platform's own message is kept."""


def sdk_version():
    try:
        return importlib.metadata.version("neoruntime-ipc-sdk")
    except importlib.metadata.PackageNotFoundError:
        return None


def default_client_factory():
    # Imported lazily so the offline tests never need the SDK.
    from neoruntime_ipc_sdk import DeviceClient

    client = DeviceClient()
    client.connect()      # the lazy connect is not thread-safe: do it here
    return client


def parse_hex(text):
    """Hex bytes with optional space, comma, colon, dash or newline marks."""
    compact = str(text).translate(_HEX_CLEANUP)
    if (not compact or len(compact) % 2
            or not all(mark in _HEXDIGITS for mark in compact)):
        raise ValueError(f"{text!r} is not an even number of hex bytes")
    return bytes.fromhex(compact)


def parse_payload(text=None, hex_=None, encoding="utf-8"):
    """Bytes of a manual TX: exactly one of a text field or a hex field."""
    if (text is None) == (hex_ is None):
        raise ValueError("give exactly one of 'text' or 'hex'")
    if hex_ is not None:
        data = parse_hex(hex_)
    else:
        try:
            data = str(text).encode(encoding)
        except LookupError as exc:
            raise ValueError(f"unknown text encoding {encoding!r}") from exc
    if not data:
        raise ValueError("the payload is empty")
    if len(data) > MAX_TX_PAYLOAD:
        raise ValueError(
            f"payload of {len(data)} bytes exceeds the {MAX_TX_PAYLOAD}-byte "
            "frame limit")
    return data


def require_int(value, lo, hi, name):
    """An int (never a bool) inside ``lo..hi``; anything else is a ValueError."""
    if (isinstance(value, bool) or not isinstance(value, int)
            or not lo <= value <= hi):
        raise ValueError(f"{name} must be an integer from {lo} to {hi}")
    return value


def require_seconds(value, name):
    """Milliseconds (a non-negative int or float) -> seconds."""
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or value < 0):
        raise ValueError(
            f"{name} must be a non-negative number of milliseconds")
    return value / 1000.0
