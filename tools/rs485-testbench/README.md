# RS-485 Testbench

A bench tool for the RS-485 RX/TX path of the NeoRuntime platform, packaged
as an app. It subscribes with `DeviceClient.subscribe_rs485_rx()`, shows every
frame the bus delivers, and judges numbered bursts, drop policies, subscriber
isolation and the echo round trip from one web page.

`peer/rs485_peer.py` is the other end of the bus. An NE503 neither loops back
nor answers on its own RS-485 port, so a PC with a USB-RS485 adapter sends
what the device must receive and watches what it transmits.

```
 PC with the adapter                      NE503
 +---------------------+   RS-485 bus   +----------+  device-control   +---------------+
 | rs485_peer.py       |  A --- A       |   MCU    |  SubscribeRs485Rx| rs485_test-   |
 |   send / echo /     |  B --- B       |   cuts   |----------------->| bench app     |
 |   monitor / raw     |  GND - GND     |   frames |    Rs485Tx       | web console   |
 +---------------------+                +----------+<-----------------+ on port 8097  |
                                          (max 512 bytes)
```

## Requirements

| Piece | Needs |
| --- | --- |
| Platform firmware | the `SubscribeRs485Rx` RPC (platform `release/v1.2.0` and later) |
| SDK | `neoruntime-ipc-sdk` 0.8.2 or later, installed into the image from PyPI |
| PC | Python 3.8+ and `pyserial`; a USB-RS485 adapter wired A to A, B to B, common ground |

## Build and install

```bash
./tools/rs485-testbench/build.sh                     # arm64 .neoapp in dist/
NEORUNTIME_SDK_VERSION=0.8.2 ./tools/rs485-testbench/build.sh
```

Import the `.neoapp` in the web console's app dialog, or unpack it and run
`aipc-cli app install rs485_testbench <dir>/app.yaml <dir>/image.tar`. Start
the app and open `http://<device>:8097/`.

## What the page does

- **Bus and stream**: init/deinit the port, pick the baud rate, the backlog
  depth and the drop policy, start the stream. *Slow consumer* adds a delay
  per frame to imitate a lagging reader.
- **Frames**: newest first, with arrival gap, length, kind (raw, vector,
  announce, pong, corrupt), sequence number and hex/text.
- **Run verdict**: for a numbered burst from the peer: PASS or FAIL with the
  received, missing, corrupt, duplicate and out-of-order counts, and whether
  the head or the tail of the burst was dropped.
- **2nd subscriber**: an independent subscription (backlog 128, never
  throttled) that must keep receiving while the main stream is slow.
- **Transmit**: one raw frame, or a numbered burst from the device that
  `rs485_peer.py monitor` checks.
- **Round trip**: pings timed against a peer in echo mode.
- **Download report**: the counters and verdicts as JSON.

## The peer script

```bash
pip install pyserial
python rs485_peer.py ports
python rs485_peer.py --port COM6 --baud 115200 send --count 200 --size 64
python rs485_peer.py --port COM6 echo
python rs485_peer.py --port COM6 monitor
python rs485_peer.py --port COM6 raw --text OPEN1
```

| Command | What it does | Where to look |
| --- | --- | --- |
| `send` | Sends an announcement and numbered vectors | the page's run verdict |
| `echo` | Sends every received frame back | the page's round-trip panel |
| `monitor` | Prints what the device sends and checks its bursts | its own `VERDICT:` line |
| `raw` | Sends one hand-typed frame, as hex or text | the page's frame table |

The receiver cuts a frame where the line stays idle, so every frame the peer
writes is followed by an idle gap (`--idle-ms`, default 20 ms).

`TESTGUIDE.zh.md` (Chinese) walks a tester through the wiring, the commands
and the test cases.

## Test-vector frame

One vector is one RS-485 frame of at most 512 bytes; the layout lives in
`rs485_testbench/vectors.py`. The peer carries its own copy of the codec
because it is one self-contained file; `tests/test_peer.py` keeps the two
byte-identical.

## Reading the numbers

- **Round trip** = device TX + bus + PC adapter + device RX. It includes the
  adapter's USB latency, so compare runs made on the same adapter instead of
  reading it as platform latency.
- **Delivery is at least once.** The MCU link re-sends an event when its
  acknowledgement is lost, so a duplicate can be legitimate after a link
  disturbance.
- **Frames are idle-delimited and at most 512 bytes.** Anything longer
  arrives as several frames.
- **The MCU queues four RX events** and drops the oldest beyond that (the
  `rs485 st` command on the MCU console reports them as `host_evt_drops`).
  Sustained rates above what the MCU-to-host link acknowledges therefore show
  up as missing sequence numbers.

## Tests

```bash
python -m pytest tools/rs485-testbench
```

Offline: a scripted DeviceClient stands in for the SDK, with one bounded
backlog per subscription as device-control has, and a fake serial port stands
in for the adapter.
