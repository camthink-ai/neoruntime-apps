"""Wire format of the RS-485 test vectors."""

import pytest

from rs485_testbench import vectors as v


def test_crc16_known_answer():
    assert v.crc16_ccitt(b"123456789") == 0x29B1


def test_crc16_of_empty_input_is_the_init_value():
    assert v.crc16_ccitt(b"") == 0xFFFF


def test_encode_lays_out_the_documented_fields():
    frame = v.encode(v.KIND_DATA, 7, b"abc")

    assert frame[:4] == v.MAGIC
    assert frame[4] == v.KIND_DATA
    assert frame[5:9] == (7).to_bytes(4, "big")
    assert frame[9:11] == (3).to_bytes(2, "big")
    assert frame[11:14] == b"abc"
    assert frame[14:] == v.crc16_ccitt(frame[:14]).to_bytes(2, "big")


@pytest.mark.parametrize("kind", [v.KIND_ANNOUNCE, v.KIND_DATA, v.KIND_PING])
@pytest.mark.parametrize("seq", [0, 1, 2**31, 2**32 - 1])
@pytest.mark.parametrize("size", [0, 1, v.MAX_PAYLOAD])
def test_encode_decode_round_trip(kind, seq, size):
    payload = v.payload_for(seq, size)

    decoded = v.decode(v.encode(kind, seq, payload))

    assert decoded == v.Vector(kind=kind, seq=seq, payload=payload)


def test_largest_vector_is_a_512_byte_frame():
    frame = v.encode(v.KIND_DATA, 1, v.payload_for(1, v.MAX_PAYLOAD))

    assert len(frame) == v.MAX_FRAME
    assert len(v.decode(frame).payload) == v.MAX_PAYLOAD


def test_payload_beyond_the_frame_limit_is_rejected():
    with pytest.raises(ValueError, match="payload"):
        v.encode(v.KIND_DATA, 1, b"\x00" * (v.MAX_PAYLOAD + 1))


@pytest.mark.parametrize("seq", [-1, 2**32])
def test_sequence_number_outside_uint32_is_rejected(seq):
    with pytest.raises(ValueError, match="seq"):
        v.encode(v.KIND_DATA, seq)


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError, match="kind"):
        v.encode(9, 1)


def test_plain_text_is_not_a_vector():
    with pytest.raises(v.VectorError, match="magic"):
        v.decode(b"hello")

    assert not v.looks_like_vector(b"hello")
    assert v.looks_like_vector(v.encode(v.KIND_DATA, 1))


def test_frame_shorter_than_the_header_is_rejected():
    with pytest.raises(v.VectorError, match="short"):
        v.decode(v.MAGIC)


def test_flipped_crc_byte_is_reported_as_crc_error():
    frame = bytearray(v.encode(v.KIND_DATA, 1, b"abc"))
    frame[-1] ^= 0x01

    with pytest.raises(v.VectorError, match="crc"):
        v.decode(bytes(frame))


def test_truncated_and_extended_frames_are_reported_as_length_error():
    frame = v.encode(v.KIND_DATA, 1, b"abc")

    with pytest.raises(v.VectorError, match="length"):
        v.decode(frame[:-1])
    with pytest.raises(v.VectorError, match="length"):
        v.decode(frame + b"\x00")


def test_unknown_kind_with_a_valid_crc_is_rejected():
    frame = bytearray(v.encode(v.KIND_DATA, 1, b"abc"))
    frame[4] = 9
    frame[11 + 3:] = v.crc16_ccitt(bytes(frame[:14])).to_bytes(2, "big")

    with pytest.raises(v.VectorError, match="kind"):
        v.decode(bytes(frame))


def test_payload_pattern_is_deterministic_and_depends_on_the_sequence():
    assert v.payload_for(5, 16) == v.payload_for(5, 16)
    assert len(v.payload_for(5, 16)) == 16
    assert v.payload_for(5, 16) != v.payload_for(6, 16)
    assert v.payload_for(5, 0) == b""


def test_announcement_round_trip():
    frame = v.encode_announcement(250)

    vec = v.decode(frame)
    assert vec.kind == v.KIND_ANNOUNCE
    assert vec.seq == 0
    assert v.decode_announcement(vec) == 250


def test_announcement_count_must_fit_uint32():
    with pytest.raises(ValueError, match="count"):
        v.encode_announcement(2**32)
    with pytest.raises(ValueError, match="count"):
        v.encode_announcement(0)


def test_announcement_decoder_rejects_other_vectors():
    with pytest.raises(ValueError, match="announcement"):
        v.decode_announcement(v.Vector(v.KIND_DATA, 1, b"\x00\x00\x00\x05"))
    with pytest.raises(ValueError, match="announcement"):
        v.decode_announcement(v.Vector(v.KIND_ANNOUNCE, 0, b"\x01"))
