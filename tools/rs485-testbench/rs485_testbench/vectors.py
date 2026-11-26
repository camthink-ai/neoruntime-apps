"""Wire format of the RS-485 test vectors.

One vector is one RS-485 frame. The MCU cuts a frame where the line goes
idle, and the host link carries at most 512 bytes per frame, so a vector
never exceeds 512 bytes:

    offset  size  field
    0       4     magic   ``R48T``
    4       1     kind    1 announcement, 2 data, 3 ping
    5       4     seq     uint32, big-endian
    9       2     length  uint16 payload length, big-endian
    11      n     payload
    11 + n  2     crc16   CRC-16/CCITT-FALSE over everything before it

``peer/rs485_peer.py`` carries its own copy of this codec because it has to
run on a PC without this package; ``tests/test_peer.py`` keeps the two in
step.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAGIC = b"R48T"
KIND_ANNOUNCE = 1
KIND_DATA = 2
KIND_PING = 3
KINDS = (KIND_ANNOUNCE, KIND_DATA, KIND_PING)

MAX_FRAME = 512
HEADER = struct.Struct(">4sBIH")
CRC = struct.Struct(">H")
_COUNT = struct.Struct(">I")
MAX_PAYLOAD = MAX_FRAME - HEADER.size - CRC.size
MAX_SEQ = 2**32 - 1


@dataclass(frozen=True)
class Vector:
    kind: int
    seq: int
    payload: bytes


class VectorError(ValueError):
    """A frame that carries the magic but is not a valid vector."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _build_table():
    table = []
    for byte in range(256):
        crc = byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) if crc & 0x8000 else crc << 1
            crc &= 0xFFFF
        table.append(crc)
    return tuple(table)


_TABLE = _build_table()


def crc16_ccitt(data: bytes) -> int:
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection)."""
    crc = 0xFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ _TABLE[((crc >> 8) ^ byte) & 0xFF]
    return crc


def payload_for(seq: int, size: int) -> bytes:
    """Deterministic payload tied to ``seq``, so both sides can check bytes."""
    return bytes((seq * 131 + index) & 0xFF for index in range(size))


def encode(kind: int, seq: int, payload: bytes = b"") -> bytes:
    if kind not in KINDS:
        raise ValueError(f"unknown vector kind {kind!r}")
    if not 0 <= seq <= MAX_SEQ:
        raise ValueError(f"seq {seq} is outside uint32")
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(
            f"payload of {len(payload)} bytes exceeds the {MAX_PAYLOAD}-byte limit")
    body = HEADER.pack(MAGIC, kind, seq, len(payload)) + payload
    return body + CRC.pack(crc16_ccitt(body))


def looks_like_vector(frame: bytes) -> bool:
    return bytes(frame[:4]) == MAGIC


def decode(frame: bytes) -> Vector:
    frame = bytes(frame)
    if len(frame) < HEADER.size:
        raise VectorError(
            "short frame" if looks_like_vector(frame) else "bad magic")
    magic, kind, seq, length = HEADER.unpack(frame[:HEADER.size])
    if magic != MAGIC:
        raise VectorError("bad magic")
    if len(frame) != HEADER.size + length + CRC.size:
        raise VectorError("length mismatch")
    if CRC.unpack(frame[-CRC.size:])[0] != crc16_ccitt(frame[:-CRC.size]):
        raise VectorError("crc mismatch")
    if kind not in KINDS:
        raise VectorError("unknown kind")
    return Vector(kind=kind, seq=seq, payload=frame[HEADER.size:-CRC.size])


def encode_announcement(count: int) -> bytes:
    """First frame of a burst: tells the receiver how many vectors follow."""
    if not 1 <= count <= MAX_SEQ:
        raise ValueError(f"announcement count {count} is outside 1..{MAX_SEQ}")
    return encode(KIND_ANNOUNCE, 0, _COUNT.pack(count))


def decode_announcement(vec: Vector) -> int:
    if vec.kind != KIND_ANNOUNCE or len(vec.payload) != _COUNT.size:
        raise ValueError("not an announcement vector")
    return _COUNT.unpack(vec.payload)[0]
