#!/usr/bin/env python3
"""RS-485 peer for the NeoRuntime RS-485 Testbench - runs on the PC.

An NE503 neither loops back nor answers on its own RS-485 port, so every RX
test needs something on the other end of the bus. This script is that
something: it sits on a USB-RS485 adapter wired to the device port (A to A,
B to B, common ground) and speaks the test-vector format of the testbench
app.

    pip install pyserial
    python rs485_peer.py ports
    python rs485_peer.py --port COM6 --baud 115200 send --count 200 --size 64
    python rs485_peer.py --port COM6 echo
    python rs485_peer.py --port COM6 monitor
    python rs485_peer.py --port COM6 raw --text OPEN1

Commands
  send     Send a numbered burst; the app on the device checks it.
  echo     Send every received frame back (the app's round-trip test needs it).
  monitor  Print what the device transmits and check its numbered bursts.
  raw      Send one hand-typed frame, as hex or as text.
  ports    List the serial ports of this PC.

Test-vector frame (at most 512 bytes, identical to
rs485_testbench/rs485_testbench/vectors.py):

    offset  size  field
    0       4     magic   R48T
    4       1     kind    1 announcement, 2 data, 3 ping
    5       4     seq     uint32, big-endian
    9       2     length  uint16 payload length, big-endian
    11      n     payload
    11 + n  2     crc16   CRC-16/CCITT-FALSE over everything before it

The receiver cuts frames where the line stays idle, so every frame written
here is followed by an idle gap (``--idle-ms``, default 20 ms).
"""

import argparse
import collections
import struct
import sys
import time

try:
    import serial
except ImportError:      # the commands need it; --help and the codec do not
    serial = None

Vector = collections.namedtuple("Vector", "kind seq payload")
MAGIC = b"R48T"
KIND_ANNOUNCE = 1
KIND_DATA = 2
KIND_PING = 3
KINDS = (KIND_ANNOUNCE, KIND_DATA, KIND_PING)

MAX_FRAME = 512
HEADER = struct.Struct(">4sBIH")
CRC = struct.Struct(">H")
COUNT = struct.Struct(">I")
MAX_PAYLOAD = MAX_FRAME - HEADER.size - CRC.size
MAX_SEQ = 2**32 - 1
MAX_RAW_PAYLOAD = 4 * MAX_FRAME   # ``raw`` may span several frames

POLL_S = 0.002            # receive loop tick
DEFAULT_IDLE_MS = 20      # line idle that separates two frames
BITS_PER_BYTE = 10        # 8N1
MISSING_SAMPLE = 50

EXIT_OK, EXIT_FAIL, EXIT_USAGE = 0, 1, 2


# --- codec: a byte-identical copy of rs485_testbench/vectors.py ------------


class VectorError(ValueError):
    """A frame that carries the magic but is not a valid vector."""

    def __init__(self, reason):
        ValueError.__init__(self, reason)
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


def crc16_ccitt(data):
    """CRC-16/CCITT-FALSE (poly 0x1021, init 0xFFFF, no reflection)."""
    crc = 0xFFFF
    for byte in data:
        crc = ((crc << 8) & 0xFFFF) ^ _TABLE[((crc >> 8) ^ byte) & 0xFF]
    return crc


def payload_for(seq, size):
    """Deterministic payload tied to ``seq``, so both sides can check bytes."""
    return bytes((seq * 131 + index) & 0xFF for index in range(size))


def encode(kind, seq, payload=b""):
    if kind not in KINDS:
        raise ValueError("unknown vector kind %r" % (kind,))
    if not 0 <= seq <= MAX_SEQ:
        raise ValueError("seq %d is outside uint32" % seq)
    payload = bytes(payload)
    if len(payload) > MAX_PAYLOAD:
        raise ValueError(
            "payload of %d bytes exceeds the %d-byte limit"
            % (len(payload), MAX_PAYLOAD))
    body = HEADER.pack(MAGIC, kind, seq, len(payload)) + payload
    return body + CRC.pack(crc16_ccitt(body))


def looks_like_vector(frame):
    return bytes(frame[:4]) == MAGIC


def decode(frame):
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
    return Vector(kind, seq, frame[HEADER.size:-CRC.size])


def encode_announcement(count):
    """First frame of a burst: tells the receiver how many vectors follow."""
    if not 1 <= count <= MAX_SEQ:
        raise ValueError(
            "announcement count %d is outside 1..%d" % (count, MAX_SEQ))
    return encode(KIND_ANNOUNCE, 0, COUNT.pack(count))


def decode_announcement(vec):
    if vec.kind != KIND_ANNOUNCE or len(vec.payload) != COUNT.size:
        raise ValueError("not an announcement vector")
    return COUNT.unpack(vec.payload)[0]


def parse_hex(text):
    """Hex bytes with optional space, comma, colon, dash or newline marks."""
    compact = "".join(
        char for char in str(text) if char not in " ,:\t\r\n-_")
    if (not compact or len(compact) % 2
            or any(char not in "0123456789abcdefABCDEF" for char in compact)):
        raise ValueError("%r is not an even number of hex bytes" % (text,))
    return bytes.fromhex(compact)


# --- line helpers ---------------------------------------------------------


def _frame_time_s(nbytes, baud):
    """Seconds a frame of ``nbytes`` occupies on the line (8N1)."""
    if baud <= 0:
        raise ValueError("baud must be positive")
    return nbytes * BITS_PER_BYTE / float(baud)


bus_time_s = _frame_time_s


def iter_frames(ser, gap_s, stop=lambda: False, clock=time.monotonic,
                sleep=time.sleep):
    """Yield frames cut where the line stays idle for ``gap_s``."""
    buf = bytearray()
    last_rx = None
    while not stop():
        waiting = ser.in_waiting
        if waiting:
            buf += ser.read(waiting)
            last_rx = clock()
            continue
        if buf and last_rx is not None and clock() - last_rx >= gap_s:
            frame = bytes(buf)
            del buf[:]
            yield frame
            continue
        sleep(POLL_S)


def send_vectors(ser, count, size, interval_ms, start_seq=1, announce=True,
                 baud=115200, idle_s=DEFAULT_IDLE_MS / 1000.0,
                 bus_time_s=None, sleep=time.sleep):
    """Write an optional announcement and ``count`` numbered data vectors.

    ``bus_time_s``, when given, replaces the frame-time estimate (tests use
    it to keep the timing deterministic)."""
    if not 1 <= count <= MAX_SEQ:
        raise ValueError("count must be from 1 to %d" % MAX_SEQ)
    if not 0 <= size <= MAX_PAYLOAD:
        raise ValueError(
            "size must be from 0 to %d bytes so the vector fits a %d-byte "
            "frame" % (MAX_PAYLOAD, MAX_FRAME))
    if not 1 <= start_seq or start_seq + count - 1 > MAX_SEQ:
        raise ValueError("start_seq must leave room for %d vectors" % count)
    interval_s = interval_ms / 1000.0
    if interval_s < 0:
        raise ValueError("interval must not be negative")
    time_of = bus_time_s if bus_time_s is not None else (
        lambda nbytes: _frame_time_s(nbytes, baud))

    frames = []
    if announce:
        frames.append(encode_announcement(count))
    frames.extend(encode(KIND_DATA, seq, payload_for(seq, size))
                  for seq in range(start_seq, start_seq + count))
    for frame in frames[:-1]:
        ser.write(frame)
        ser.flush()
        sleep(max(interval_s, time_of(len(frame)) + idle_s))
    ser.write(frames[-1])
    ser.flush()
    return count


class Monitor(object):
    """Checks the numbered bursts the device sends (its TX path)."""

    def __init__(self):
        self.frames = 0
        self.raw = self.ok = self.corrupt = 0
        self.dup = self.out_of_order = 0
        self.expected = None
        self.seen = set()
        self.low = None
        self.high = 0

    def on_frame(self, frame):
        """Account for one frame; returns (kind, sequence number or None)."""
        self.frames += 1
        if not looks_like_vector(frame):
            self.raw += 1
            return "raw", None
        try:
            vec = decode(frame)
        except VectorError:
            self.corrupt += 1
            return "corrupt", None
        kind, seq, _payload = vec
        if kind == KIND_ANNOUNCE:
            self.expected = decode_announcement(vec)
            self.seen.clear()
            self.low, self.high = None, 0
            return "announce", seq
        if kind == KIND_PING:
            return "ping", seq
        if seq in self.seen:
            self.dup += 1
        else:
            self.seen.add(seq)
            self.ok += 1
            self.low = seq if self.low is None else min(self.low, seq)
            self.high = max(self.high, seq)
        if seq < self.high:
            self.out_of_order += 1
        return "data", seq

    def result(self):
        if self.expected is not None:
            wanted = range(1, self.expected + 1)
        elif self.low is None:
            wanted = range(0)
        else:
            wanted = range(self.low, self.high + 1)
        missing = sorted(set(wanted) - self.seen)
        if not self.ok and not self.corrupt:
            verdict = "none"          # nothing numbered arrived at all
        elif missing or self.dup or self.out_of_order or self.corrupt:
            verdict = "fail"
        else:
            verdict = "pass"
        return {
            "frames": self.frames,
            "expected": self.expected,
            "ok": self.ok,
            "corrupt": self.corrupt,
            "raw": self.raw,
            "dup": self.dup,
            "out_of_order": self.out_of_order,
            "missing": missing[:MISSING_SAMPLE],
            "missing_count": len(missing),
            "verdict": verdict,
        }


def format_summary(mon):
    """Human-readable verdict lines for what ``mon`` has seen so far."""
    result = mon.result()
    lines = [
        "frames=%d vectors=%d raw=%d corrupt=%d"
        % (result["frames"], result["ok"], result["raw"], result["corrupt"]),
        "expected=%s missing=%d dup=%d out-of-order=%d"
        % (result["expected"] if result["expected"] is not None else "n/a",
           result["missing_count"], result["dup"], result["out_of_order"]),
        "VERDICT: %s" % result["verdict"].upper(),
    ]
    if result["verdict"] == "none":
        lines.append("no numbered vectors arrived: start a burst on the "
                     "device page first")
    if result["missing"]:
        tail = " ..." if result["missing_count"] > len(result["missing"]) \
            else ""
        lines.append("missing seq: %s%s"
                     % (" ".join(str(seq) for seq in result["missing"]), tail))
    return lines


def stamp():
    now = time.time()
    return "%s.%03d" % (time.strftime("%H:%M:%S", time.localtime(now)),
                       int(now * 1000) % 1000)


def describe(frame, kind, seq):
    text = "".join(chr(byte) if 32 <= byte < 127 else "."
                   for byte in frame[:48])
    label = "seq=%-6d" % seq if seq is not None else "%-10s" % ""
    return "%s  %-8s len=%-4d %s %s  %s" % (
        stamp(), kind, len(frame), label, frame[:24].hex(), text)


# --- serial port ----------------------------------------------------------


def list_ports():
    if serial is None:
        return []
    from serial.tools import list_ports as _list_ports

    return [(port.device, port.description) for port in _list_ports.comports()]


def open_port(args):
    ser = serial.Serial(port=args.port, baudrate=args.baud, bytesize=8,
                        parity="N", stopbits=1, timeout=0, write_timeout=2)
    ser.reset_input_buffer()
    return ser


# --- commands -------------------------------------------------------------


def cmd_send(ser, args, idle_s):
    started = time.time()
    sent = send_vectors(ser, args.count, args.size, args.interval_ms,
                        args.start_seq, announce=not args.no_announce,
                        baud=args.baud, idle_s=idle_s)
    print("sent %d vector(s) of %d bytes, seq %d..%d%s"
          % (sent, args.size, args.start_seq, args.start_seq + sent - 1,
             "" if not args.no_announce else " (no announcement)"))
    print("took %.1f s; the device page shows the verdict about 2 s after "
          "the last frame" % (time.time() - started))
    return EXIT_OK


def cmd_echo(ser, idle_s):
    echoed = 0
    try:
        for frame in iter_frames(ser, gap_s=idle_s):
            ser.write(frame)
            ser.flush()
            echoed += 1
            print("%s  echoed len=%-4d %s"
                  % (stamp(), len(frame), frame[:24].hex()))
    finally:
        print("echoed %d frame(s)" % echoed)
    return EXIT_OK


def cmd_monitor(ser, idle_s, duration_s, quiet):
    mon = Monitor()
    deadline = None if duration_s is None else time.monotonic() + duration_s

    def expired():
        return deadline is not None and time.monotonic() >= deadline

    try:
        for frame in iter_frames(ser, gap_s=idle_s, stop=expired):
            kind, seq = mon.on_frame(frame)
            if not quiet:
                print(describe(frame, kind, seq))
    except KeyboardInterrupt:
        pass                # Ctrl+C ends the capture; the verdict still counts
    finally:
        for line in format_summary(mon):
            print(line)
    return EXIT_OK if mon.result()["verdict"] == "pass" else EXIT_FAIL


def cmd_raw(ser, payload, args, idle_s):
    ser.write(payload)
    ser.flush()
    time.sleep(bus_time_s(len(payload), args.baud) + idle_s)
    print("sent %d byte(s): %s" % (len(payload), payload.hex()))
    return EXIT_OK


# --- command line ---------------------------------------------------------


def _positive_int(text):
    value = int(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


def _non_negative_int(text):
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return value


def build_parser():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--port", metavar="PORT",
        help="serial port (COM6 on Windows, /dev/ttyUSB0 on Linux); "
             "without it the ports are listed")
    parser.add_argument("--baud", type=_positive_int, default=115200,
                        help="baud rate, must match the app (default: %(default)s)")
    parser.add_argument(
        "--idle-ms", type=_non_negative_int, default=DEFAULT_IDLE_MS,
        help="line idle that separates two frames (default: %(default)s ms)")
    commands = parser.add_subparsers(dest="command")
    commands.required = True

    commands.add_parser("ports", help="list the serial ports of this PC")

    send = commands.add_parser("send", help="send a numbered burst")
    send.add_argument("--count", type=_positive_int, default=100,
                      help="number of vectors (default: %(default)s)")
    send.add_argument("--size", type=_non_negative_int, default=32,
                      help="payload bytes per vector (default: %(default)s)")
    send.add_argument("--interval-ms", type=_non_negative_int, default=20,
                      help="minimum gap between frames, never shorter than "
                           "the frame's time on the line plus the idle gap "
                           "(default: %(default)s ms)")
    send.add_argument("--start-seq", type=_positive_int, default=1,
                      help="sequence number of the first vector "
                           "(default: %(default)s)")
    send.add_argument("--no-announce", action="store_true",
                      help="send data vectors only")

    commands.add_parser("echo", help="send every received frame back")

    monitor = commands.add_parser(
        "monitor", help="print what the device transmits and check it")
    monitor.add_argument("--duration", type=_positive_int, default=None,
                         help="stop after this many seconds "
                              "(default: until Ctrl+C)")
    monitor.add_argument("--quiet", action="store_true",
                         help="print the summary only")

    raw = commands.add_parser("raw", help="send one hand-typed frame")
    payload = raw.add_mutually_exclusive_group(required=True)
    payload.add_argument("--hex", help="frame bytes in hex, e.g. \"01 03 ff\"")
    payload.add_argument("--text", help="frame bytes as text")
    raw.add_argument("--encoding", default="utf-8",
                     help="text encoding for --text (default: %(default)s)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    if args.command == "ports":
        ports = list_ports()
        for name, description in ports:
            print("%-24s %s" % (name, description))
        if not ports:
            print("no serial ports found")
            return EXIT_USAGE
        return EXIT_OK

    if serial is None:
        print("pyserial is missing: pip install pyserial", file=sys.stderr)
        return EXIT_USAGE
    if not args.port:
        print("no --port given; the serial ports of this PC are:",
              file=sys.stderr)
        for name, description in list_ports():
            print("%-24s %s" % (name, description))
        return EXIT_USAGE

    payload = None
    if args.command == "raw":
        try:
            payload = (parse_hex(args.hex) if args.hex is not None
                       else args.text.encode(args.encoding))
        except (ValueError, LookupError) as exc:
            print("invalid raw payload: %s" % exc, file=sys.stderr)
            return EXIT_USAGE
        if not payload:
            print("the raw payload is empty", file=sys.stderr)
            return EXIT_USAGE
        if len(payload) > MAX_RAW_PAYLOAD:
            print("the raw payload exceeds %d bytes" % MAX_RAW_PAYLOAD,
                  file=sys.stderr)
            return EXIT_USAGE
        if len(payload) > MAX_FRAME:
            print("note: longer than one %d-byte frame; the device delivers "
                  "it as several frames" % MAX_FRAME)

    try:
        ser = open_port(args)
    except Exception as exc:        # every pyserial failure is a usage error
        print("cannot open %s: %s" % (args.port, exc), file=sys.stderr)
        return EXIT_USAGE

    idle_s = args.idle_ms / 1000.0
    try:
        if args.command == "send":
            return cmd_send(ser, args, idle_s)
        if args.command == "echo":
            return cmd_echo(ser, idle_s)
        if args.command == "monitor":
            return cmd_monitor(ser, idle_s, args.duration, args.quiet)
        return cmd_raw(ser, payload, args, idle_s)
    except KeyboardInterrupt:
        print("\ninterrupted")
        return EXIT_OK
    except Exception as exc:        # serial errors surface with their text
        print("serial port failed: %s" % exc, file=sys.stderr)
        return EXIT_FAIL
    finally:
        ser.close()


if __name__ == "__main__":
    sys.exit(main())
