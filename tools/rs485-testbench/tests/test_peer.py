"""The PC-side peer script: codec parity, framing, sending and monitoring.

peer/rs485_peer.py is one self-contained file (a tester copies nothing else
to the PC), so it carries its own copy of the vector codec. The parity tests
pin that copy to rs485_testbench.vectors.
"""

import argparse
import re
import types

import pytest

from rs485_testbench import vectors as v


class FakeSerial:
    """pyserial stand-in: queued receive chunks and recorded writes."""

    def __init__(self, chunks=()):
        self.chunks = list(chunks)   # bytes become readable one chunk at a time
        self.writes = []
        self.open = True

    @property
    def in_waiting(self):
        return len(self.chunks[0]) if self.chunks else 0

    def read(self, n):
        chunk = self.chunks.pop(0)
        assert n >= len(chunk)
        return chunk

    def write(self, data):
        self.writes.append(bytes(data))
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        pass

    def close(self):
        self.open = False


class ScheduledSerial(FakeSerial):
    """Bytes appear on the line at scheduled times, read through ``now``."""

    def __init__(self, arrivals):
        super().__init__()
        self.arrivals = sorted(arrivals)
        self.line = bytearray()
        self.now = 0.0

    def _advance(self):
        while self.arrivals and self.arrivals[0][0] <= self.now:
            self.line += self.arrivals.pop(0)[1]

    @property
    def in_waiting(self):
        self._advance()
        return len(self.line)

    def read(self, n):
        self._advance()
        out = bytes(self.line[:n])
        del self.line[:n]
        return out


# --- codec parity ----------------------------------------------------------


def test_peer_crc_matches_the_reference_and_the_known_answer(peer_mod):
    assert peer_mod.crc16_ccitt(b"123456789") == 0x29B1
    for blob in (b"", b"R48T", bytes(range(256)), b"\xff" * 500):
        assert peer_mod.crc16_ccitt(blob) == v.crc16_ccitt(blob)


@pytest.mark.parametrize("kind", [v.KIND_ANNOUNCE, v.KIND_DATA, v.KIND_PING])
@pytest.mark.parametrize("seq", [0, 1, 777, 2**32 - 1])
@pytest.mark.parametrize("size", [0, 1, 64, v.MAX_PAYLOAD])
def test_peer_encodes_identical_vectors(peer_mod, kind, seq, size):
    payload = v.payload_for(seq, size)

    assert peer_mod.payload_for(seq, size) == payload
    assert peer_mod.encode(kind, seq, payload) == v.encode(kind, seq, payload)


def test_peer_decodes_what_the_app_encodes_and_vice_versa(peer_mod):
    app_frame = v.encode(v.KIND_DATA, 42, b"payload")
    peer_frame = peer_mod.encode_announcement(9)

    assert peer_mod.decode(app_frame).payload == b"payload"
    assert v.decode_announcement(v.decode(peer_frame)) == 9
    assert peer_mod.decode_announcement(peer_mod.decode(peer_frame)) == 9


def test_peer_reports_a_corrupt_vector(peer_mod):
    broken = bytearray(peer_mod.encode(v.KIND_DATA, 1, b"x"))
    broken[-1] ^= 0x80

    with pytest.raises(peer_mod.VectorError, match="crc"):
        peer_mod.decode(bytes(broken))
    assert peer_mod.looks_like_vector(b"plain text") is False


def _kind_nine_frame(codec):
    frame = bytearray(codec.encode(codec.KIND_DATA, 1, b"abc"))
    frame[4] = 9
    body = bytes(frame[:14])
    frame[11 + 3:] = codec.crc16_ccitt(body).to_bytes(2, "big")
    return bytes(frame)


@pytest.mark.parametrize("frame, reason", [
    (b"R48T", "short"),
    (b"hello", "magic"),
    (lambda codec: codec.encode(codec.KIND_DATA, 1, b"abc")[:-1], "length"),
    (lambda codec: codec.encode(codec.KIND_DATA, 1, b"abc") + b"\x00",
     "length"),
    (_kind_nine_frame, "kind"),
])
def test_peer_rejects_what_the_app_rejects(peer_mod, frame, reason):
    for codec in (peer_mod, v):
        bad = frame(codec) if callable(frame) else frame
        with pytest.raises(codec.VectorError, match=reason):
            codec.decode(bad)


def test_monitor_recognises_pings_and_skips_them_in_the_verdict(peer_mod):
    mon = peer_mod.Monitor()

    kinds = [mon.on_frame(frame) for frame in (
        peer_mod.encode(v.KIND_PING, 4, b""),
        peer_mod.encode_announcement(1),
        peer_mod.encode(v.KIND_DATA, 1, peer_mod.payload_for(1, 4)))]

    assert kinds == [("ping", 4), ("announce", 0), ("data", 1)]
    assert mon.result()["verdict"] == "pass"


def test_monitor_without_numbered_vectors_reports_none(peer_mod):
    mon = peer_mod.Monitor()
    mon.on_frame(b"just text")

    result = mon.result()

    assert result["verdict"] == "none"
    assert any("no numbered vectors" in line
               for line in peer_mod.format_summary(mon))


# --- framing ---------------------------------------------------------------


def test_frames_are_cut_at_idle_gaps(peer_mod):
    ser = ScheduledSerial([(0.00, b"ab"), (0.01, b"cd"), (0.05, b"efg")])

    def wait(_seconds):
        ser.now += 0.01

    frames = peer_mod.iter_frames(ser, gap_s=0.02, clock=lambda: ser.now,
                                  sleep=wait)

    assert next(frames) == b"abcd"
    assert next(frames) == b"efg"


def test_framing_stops_when_asked(peer_mod):
    ser = FakeSerial([b"a"])

    frames = list(peer_mod.iter_frames(
        ser, gap_s=0.02, stop=lambda: True, clock=lambda: 0.0,
        sleep=lambda _s: None))

    assert frames == []


# --- sending ---------------------------------------------------------------


def test_send_writes_an_announcement_then_numbered_vectors(peer_mod):
    ser = FakeSerial()

    sent = peer_mod.send_vectors(ser, count=3, size=16, interval_ms=0,
                                 start_seq=7, announce=True,
                                 bus_time_s=lambda _n: 0.0,
                                 sleep=lambda _s: None)

    assert sent == 3
    assert len(ser.writes) == 4
    assert peer_mod.decode_announcement(peer_mod.decode(ser.writes[0])) == 3
    decoded = [peer_mod.decode(frame) for frame in ser.writes[1:]]
    assert [vec.seq for vec in decoded] == [7, 8, 9]
    assert all(vec.payload == v.payload_for(vec.seq, 16) for vec in decoded)


def test_send_without_announcement_emits_only_data(peer_mod):
    ser = FakeSerial()

    peer_mod.send_vectors(ser, count=2, size=4, interval_ms=0, start_seq=1,
                          announce=False, bus_time_s=lambda _n: 0.0,
                          sleep=lambda _s: None)

    assert len(ser.writes) == 2
    assert all(peer_mod.decode(f).kind == v.KIND_DATA for f in ser.writes)


def test_send_spaces_frames_by_bus_time_plus_idle(peer_mod):
    ser = FakeSerial()
    sleeps = []

    peer_mod.send_vectors(ser, count=2, size=16, interval_ms=0, start_seq=1,
                          announce=True, bus_time_s=lambda n: n / 1000.0,
                          sleep=sleeps.append, idle_s=0.02)

    assert len(ser.writes) == 3
    assert len(sleeps) == 2
    assert all(0.02 < value < 0.06 for value in sleeps)


def test_send_honours_a_longer_requested_interval(peer_mod):
    ser = FakeSerial()
    sleeps = []

    peer_mod.send_vectors(ser, count=1, size=16, interval_ms=100, start_seq=1,
                          announce=True, bus_time_s=lambda _n: 0.0,
                          sleep=sleeps.append, idle_s=0.02)

    assert sleeps == [0.1]


def test_send_rejects_a_vector_that_cannot_fit_the_frame(peer_mod):
    with pytest.raises(ValueError, match="size"):
        peer_mod.send_vectors(FakeSerial(), count=1, size=v.MAX_PAYLOAD + 1,
                              interval_ms=0)


# --- monitor ---------------------------------------------------------------


def test_monitor_passes_a_complete_in_order_burst(peer_mod):
    mon = peer_mod.Monitor()
    frames = [peer_mod.encode_announcement(3)] + [
        peer_mod.encode(v.KIND_DATA, s, peer_mod.payload_for(s, 8))
        for s in (1, 2, 3)]

    for frame in frames:
        mon.on_frame(frame)
    result = mon.result()

    assert result["verdict"] == "pass"
    assert result["frames"] == 4
    assert result["ok"] == 3
    assert result["missing"] == []


def test_monitor_fails_on_gaps_corruption_and_plain_frames(peer_mod):
    mon = peer_mod.Monitor()
    broken = bytearray(
        peer_mod.encode(v.KIND_DATA, 2, peer_mod.payload_for(2, 8)))
    broken[5] ^= 0x01                              # seq no longer matches CRC
    frames = [peer_mod.encode_announcement(4),
              peer_mod.encode(v.KIND_DATA, 1, peer_mod.payload_for(1, 8)),
              bytes(broken), b"raw bytes",
              peer_mod.encode(v.KIND_DATA, 4, peer_mod.payload_for(4, 8))]

    for frame in frames:
        mon.on_frame(frame)
    result = mon.result()

    assert result["verdict"] == "fail"
    assert result["frames"] == 5
    assert result["ok"] == 2
    assert result["corrupt"] == 1
    assert result["raw"] == 1
    assert result["missing"] == [2, 3]


def test_monitor_counts_duplicates_and_reordering(peer_mod):
    mon = peer_mod.Monitor()

    for seq in (2, 1, 2, 3):
        mon.on_frame(peer_mod.encode(v.KIND_DATA, seq,
                                     peer_mod.payload_for(seq, 4)))

    result = mon.result()
    assert result["dup"] == 1
    assert result["out_of_order"] == 1
    assert result["verdict"] == "fail"


def test_monitor_summary_is_readable_text(peer_mod):
    mon = peer_mod.Monitor()
    mon.on_frame(peer_mod.encode_announcement(1))
    mon.on_frame(peer_mod.encode(v.KIND_DATA, 1, peer_mod.payload_for(1, 4)))

    lines = peer_mod.format_summary(mon)

    assert any("PASS" in line for line in lines)
    assert any("expected=1" in line for line in lines)


def test_monitor_result_without_announcement_passes_when_clean(peer_mod):
    mon = peer_mod.Monitor()

    for seq in (5, 6):
        mon.on_frame(peer_mod.encode(v.KIND_DATA, seq,
                                     peer_mod.payload_for(seq, 4)))

    result = mon.result()
    assert result["expected"] is None
    assert result["verdict"] == "pass"


# --- command line ----------------------------------------------------------


def test_cli_parses_send_options(peer_mod):
    args = peer_mod.build_parser().parse_args(
        ["--port", "COM6", "--baud", "9600", "send", "--count", "10",
         "--size", "32", "--interval-ms", "5"])

    assert (args.port, args.baud, args.command) == ("COM6", 9600, "send")
    assert (args.count, args.size, args.interval_ms) == (10, 32, 5)


def test_cli_rejects_conflicting_raw_payload_options(peer_mod):
    parser = peer_mod.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--port", "COM6", "raw", "--hex", "01",
                           "--text", "a"])


def test_cli_rejects_a_negative_interval(peer_mod):
    parser = peer_mod.build_parser()

    with pytest.raises(SystemExit):
        parser.parse_args(["--port", "COM6", "send", "--interval-ms", "-1"])


def test_cli_defaults_when_no_port_is_given(peer_mod):
    args = peer_mod.build_parser().parse_args(["send"])

    assert args.port is None
    assert args.idle_ms > 0


def test_main_without_pyserial_exits_with_a_hint(peer_mod, monkeypatch, capsys):
    monkeypatch.setattr(peer_mod, "serial", None)

    code = peer_mod.main(["--port", "COM6", "send", "--count", "1"])

    assert code == 2
    assert "pyserial" in capsys.readouterr().err


def test_main_reports_a_bad_hex_payload(peer_mod, monkeypatch, capsys):
    monkeypatch.setattr(peer_mod, "serial", object())

    code = peer_mod.main(["--port", "COM6", "raw", "--hex", "zz"])

    assert code == 2
    assert "hex" in capsys.readouterr().err


def test_main_opens_the_port_and_sends(peer_mod, monkeypatch):
    ser = FakeSerial()
    monkeypatch.setattr(peer_mod, "open_port", lambda _args: ser)

    code = peer_mod.main(["--port", "COM6", "send", "--count", "2",
                          "--size", "8", "--interval-ms", "0"])

    assert code == 0
    assert len(ser.writes) == 3


def test_main_lists_ports_when_asked(peer_mod, monkeypatch, capsys):
    monkeypatch.setattr(peer_mod, "list_ports",
                        lambda: [("COM6", "USB-RS485")])

    code = peer_mod.main(["ports"])

    assert code == 0
    assert "COM6" in capsys.readouterr().out


def test_main_without_a_port_lists_them_and_exits(peer_mod, monkeypatch, capsys):
    monkeypatch.setattr(peer_mod, "list_ports",
                        lambda: [("COM6", "USB-RS485")])

    code = peer_mod.main(["send", "--count", "1"])

    assert code == 2
    assert "COM6" in capsys.readouterr().out


def test_parse_hex_payload(peer_mod):
    assert peer_mod.parse_hex("01 02 ff") == b"\x01\x02\xff"
    with pytest.raises(ValueError):
        peer_mod.parse_hex("abc")
    with pytest.raises(ValueError):
        peer_mod.parse_hex("zz")


def test_bus_time_for_a_512_byte_frame_at_9600_baud(peer_mod):
    frame = peer_mod.encode(v.KIND_DATA, 1, peer_mod.payload_for(1, v.MAX_PAYLOAD))

    assert peer_mod.bus_time_s(len(frame), 9600) == pytest.approx(
        512 * 10 / 9600, rel=1e-6)
    assert peer_mod.bus_time_s(len(peer_mod.encode_announcement(1)), 9600) > 0


def test_bus_time_edge_cases(peer_mod):
    assert peer_mod.bus_time_s(0, 115200) == 0.0
    with pytest.raises(ValueError):
        peer_mod.bus_time_s(10, 0)


def test_send_rejects_bad_arguments(peer_mod):
    ser = FakeSerial()

    for count, interval_ms, start_seq in ((0, 0, 1), (1, -5, 1), (1, 0, 0)):
        with pytest.raises(ValueError):
            peer_mod.send_vectors(ser, count, 8, interval_ms, start_seq)

    assert ser.writes == []


def test_stamp_has_millisecond_precision(peer_mod):
    assert re.match(r"^\d{2}:\d{2}:\d{2}\.\d{3}$", peer_mod.stamp())


def test_describe_names_the_frame_and_its_bytes(peer_mod):
    frame = peer_mod.encode(v.KIND_DATA, 7, b"ab")

    line = peer_mod.describe(frame, "data", 7)

    assert "data" in line and "seq=7" in line
    assert "len=%d" % len(frame) in line
    assert frame[:24].hex() in line
    assert "R48T" in line


def frames_then_interrupt(frames):
    def fake(_ser, gap_s, stop=lambda: False):
        yield from frames
        raise KeyboardInterrupt

    return fake


def test_main_send_reports_what_went_out(peer_mod, monkeypatch, capsys):
    ser = FakeSerial()
    monkeypatch.setattr(peer_mod, "open_port", lambda _args: ser)

    code = peer_mod.main(["--port", "COM6", "send", "--count", "2",
                          "--size", "4", "--no-announce"])

    assert code == 0
    assert ("sent 2 vector(s) of 4 bytes, seq 1..2 (no announcement)"
            in capsys.readouterr().out)


def run_with_port(peer_mod, monkeypatch, argv, frames):
    """Run ``argv`` through main() on a fake port; ``frames`` arrive, then Ctrl+C."""
    ser = FakeSerial()
    monkeypatch.setattr(peer_mod, "open_port", lambda _args: ser)
    monkeypatch.setattr(peer_mod, "iter_frames", frames_then_interrupt(frames))
    code = peer_mod.main(["--port", "COM6"] + argv)
    return code, ser


def test_echo_sends_every_frame_back(peer_mod, monkeypatch, capsys):
    code, ser = run_with_port(peer_mod, monkeypatch, ["echo"],
                              [b"one", b"two"])

    assert code == 0
    assert ser.writes == [b"one", b"two"]
    assert "echoed 2 frame(s)" in capsys.readouterr().out


def test_monitor_prints_frames_and_a_verdict(peer_mod, monkeypatch, capsys):
    frames = [peer_mod.encode_announcement(2)] + [
        peer_mod.encode(v.KIND_DATA, seq, peer_mod.payload_for(seq, 4))
        for seq in (1, 2)]

    code, _ser = run_with_port(peer_mod, monkeypatch, ["monitor"], frames)

    out = capsys.readouterr().out
    assert code == 0
    assert "VERDICT: PASS" in out
    assert "announce" in out and "data" in out


def test_monitor_quiet_prints_the_summary_and_fails_a_bad_burst(
        peer_mod, monkeypatch, capsys):
    frames = [peer_mod.encode_announcement(2),
              peer_mod.encode(v.KIND_DATA, 2, peer_mod.payload_for(2, 4))]

    code, _ser = run_with_port(peer_mod, monkeypatch,
                               ["monitor", "--quiet"], frames)

    out = capsys.readouterr().out
    assert code == 1
    assert "VERDICT: FAIL" in out
    assert "missing seq: 1" in out
    assert "announce" not in out


def test_monitor_stops_when_the_duration_elapses(peer_mod, monkeypatch,
                                                 capsys):
    def fake(_ser, gap_s, stop=lambda: False):
        for frame in (b"a", b"b", b"c"):
            if stop():
                return
            yield frame

    monkeypatch.setattr(peer_mod, "iter_frames", fake)
    monkeypatch.setattr(peer_mod, "open_port", lambda _args: FakeSerial())
    # deadline is taken at 10.0; the two frames below pass before 11.0,
    # the third one is refused once the clock reads 11.5.
    ticks = iter([10.0, 10.5, 10.8, 11.5])
    monkeypatch.setattr(peer_mod.time, "monotonic", lambda: next(ticks))

    code = peer_mod.main(["--port", "COM6", "monitor", "--duration", "1",
                          "--quiet"])

    assert code == 1
    assert "frames=2" in capsys.readouterr().out


def test_open_port_configures_8n1_and_clears_the_input(peer_mod, monkeypatch):
    made = {}

    class Port:
        def reset_input_buffer(self):
            made["reset"] = True

    def serial_port(**kwargs):
        made.update(kwargs)
        return Port()

    monkeypatch.setattr(peer_mod, "serial",
                        types.SimpleNamespace(Serial=serial_port))

    port = peer_mod.open_port(argparse.Namespace(port="COM6", baud=19200))

    assert made == {"port": "COM6", "baudrate": 19200, "bytesize": 8,
                    "parity": "N", "stopbits": 1, "timeout": 0,
                    "write_timeout": 2, "reset": True}
    assert isinstance(port, Port)


def test_main_cannot_open_the_port(peer_mod, monkeypatch, capsys):
    def refuse(_args):
        raise OSError("permission denied")

    monkeypatch.setattr(peer_mod, "open_port", refuse)

    code = peer_mod.main(["--port", "COM6", "echo"])

    assert code == 2
    assert "cannot open COM6" in capsys.readouterr().err


def test_main_reports_a_serial_failure(peer_mod, monkeypatch, capsys):
    class Broken(FakeSerial):
        def write(self, data):
            raise OSError("write failed")

    monkeypatch.setattr(peer_mod, "open_port", lambda _args: Broken())

    code = peer_mod.main(["--port", "COM6", "raw", "--text", "OPEN1"])

    assert code == 1
    assert "serial port failed" in capsys.readouterr().err


@pytest.mark.parametrize("argv, expected", [
    (["raw", "--text", "OPEN1"], b"OPEN1"),
    (["raw", "--hex", "01 03 ff"], b"\x01\x03\xff"),
])
def test_main_raw_sends_text_and_hex(peer_mod, monkeypatch, argv, expected):
    ser = FakeSerial()
    monkeypatch.setattr(peer_mod, "open_port", lambda _args: ser)

    assert peer_mod.main(["--port", "COM6"] + argv) == 0

    assert ser.writes == [expected]


def test_main_rejects_an_empty_or_oversized_raw_payload(peer_mod, capsys):
    limit = peer_mod.MAX_RAW_PAYLOAD

    assert peer_mod.main(["--port", "COM6", "raw", "--text", ""]) == 2
    assert peer_mod.main(["--port", "COM6", "raw", "--text", "x" * (limit + 1)]) == 2
    assert "raw payload" in capsys.readouterr().err


def test_main_raw_longer_than_a_frame_is_split_with_a_note(peer_mod,
                                                           monkeypatch,
                                                           capsys):
    ser = FakeSerial()
    monkeypatch.setattr(peer_mod, "open_port", lambda _args: ser)
    payload = b"x" * (peer_mod.MAX_FRAME + 10)

    code = peer_mod.main(["--port", "COM6", "raw", "--hex", payload.hex()])

    assert code == 0
    assert ser.writes == [payload]
    assert "several frames" in capsys.readouterr().out


def test_main_lists_nothing_without_pyserial(peer_mod, monkeypatch, capsys):
    monkeypatch.setattr(peer_mod, "serial", None)

    assert peer_mod.main(["ports"]) == 2
    assert "no serial ports found" in capsys.readouterr().out
    assert peer_mod.list_ports() == []


def test_main_requires_a_command(peer_mod):
    with pytest.raises(SystemExit):
        peer_mod.main([])
