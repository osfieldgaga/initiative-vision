# Sprint 2 walkthrough: CAN logging

Written 2026-09-23. Same goal as `sprint-1-frames.md`: read this once and you should be able to write, rewrite, or debug any part of this yourself. This one's shorter — the CAN pipeline deliberately reuses almost everything from Sprint 1, so most of the interesting content is *the differences*, not new machinery.

Read `sprint-1-frames.md` first if you haven't — this assumes you already know what `Session`, `DurableCsvWriter`, and the capture→queue→writer shape are for.

---

## 1. The big picture

**What it does, in one sentence:** open a raw socket on a CAN interface, and for every frame the kernel delivers, save one row in a CSV — forever, until told to stop, and *without ever transmitting anything*.

**One frame's journey:**

```
kernel (SocketCAN driver)
    |  recvmsg() -- returns the 16 raw bytes of a struct can_frame
    v
capture thread (pipeline.py: _capture_loop)
    |  decode + timestamp
    |  hands off via a bounded queue (never blocks)
    v
writer thread (pipeline.py: _writer_loop)
    |  append a row to can.csv
    v
disk
```

Notice what's *missing* compared to frames: no encode stage. A CAN frame is already a complete, tiny record (at most 8 data bytes) — there's nothing to compress or transform, so the pipeline is capture → queue → write, three stages instead of four.

**Module map:**

| File | One line |
|---|---|
| `can/source.py` | The `CanFrame` type, the `CanSource` interface, the real SocketCAN reader, a fake source for testing |
| `can/pipeline.py` | `CanRecorder`: two threads, the queue, drop accounting, shutdown |
| `can/record.py` | The CLI: wires everything together |

That's the whole package. Compare to Sprint 1's eleven files — this is genuinely smaller, both because there's no encode stage and because it reuses `Session` and `DurableCsvWriter` unchanged rather than needing its own.

### Reading order

```
Layer 0    can/source.py       (needs nothing internal)
              |
Layer 1    can/pipeline.py     (needs csvlog, session, source)
              |
Layer 2    can/record.py       (needs pipeline, source)
```

Read `source.py` first, all of it — it's one file, and everything in `pipeline.py` only makes sense once you know what a `CanFrame` is and what `SocketCanSource.frames()` actually yields. Then `pipeline.py`. Then `record.py`, which by that point is just wiring, exactly like `frames/record.py` was.

---

## 2. `can/source.py` — reading the bus

### The wire format

A `struct can_frame` (defined by the Linux kernel, `linux/can.h`) is exactly 16 bytes: a 4-byte ID, a 1-byte length, 3 padding bytes, and 8 bytes of data (even if the message uses fewer — the length field, `dlc`, says how many are real).

```python
_CAN_FRAME_FMT = "=IB3x8s"   # u32 can_id, u8 dlc, 3 pad bytes, 8 bytes data
CAN_FRAME_SIZE = struct.calcsize(_CAN_FRAME_FMT)
assert CAN_FRAME_SIZE == 16
```

That `assert` isn't decoration — this layout was verified by actually sending and receiving a frame over a real socket and checking the byte count came back as 16, not taken on faith from a header file. The `=` prefix matters: it means "native byte order, no padding," which is what the kernel actually puts on the wire; without it Python's `struct` module might insert alignment padding of its own and silently misparse every frame.

### The ID isn't just an ID

The 32-bit `can_id` field packs three flag bits into its top bits alongside the actual arbitration ID:

```python
CAN_EFF_FLAG = 0x80000000  # extended (29-bit) ID, not standard (11-bit)
CAN_RTR_FLAG = 0x40000000  # remote transmission request -- a request, not data
CAN_EFF_MASK = 0x1FFFFFFF
CAN_SFF_MASK = 0x000007FF
```

`_decode_can_frame()` checks the flag bits, then masks them off before storing `can_id` — so `frame.can_id` is always just the arbitration ID, and `frame.is_extended` / `frame.is_remote_frame` tell you the rest. Get the mask wrong and you'd either lose high bits off a real extended ID or leave flag bits mixed into what should be a clean ID — this is exactly the kind of bug that looks fine in testing (small IDs happen to work) and breaks the moment a real extended-ID message shows up.

### `CanFrame` — and why it has *two* timestamp fields

```python
@dataclass(frozen=True)
class CanFrame:
    timestamp_ns: int                 # our own CLOCK_MONOTONIC read
    kernel_realtime_ns: int | None    # the kernel's own CLOCK_REALTIME read
    can_id: int
    is_extended: bool
    is_remote_frame: bool
    dlc: int
    data: bytes
```

This is the one genuinely new idea in this sprint, so it's worth slowing down on. SocketCAN can hand you a kernel-generated receive timestamp via a socket option (`SO_TIMESTAMPNS`) — free precision you don't have to generate yourself in Python. The obvious move is to just use that as the frame's timestamp. **I checked what clock it's actually on before doing that, rather than assuming**, and it's `CLOCK_REALTIME` — wall-clock time, the same clock that jumps when NTP corrects the Pi's clock. Sprint 1's whole timestamp convention (`docs/timestamp-convention.md`) exists specifically to avoid depending on that clock, because a Pi with no RTC can have a wildly wrong `CLOCK_REALTIME` for a while after boot.

So: `timestamp_ns` (the field every other part of the system reads) is **our own** `time.monotonic_ns()`, read in Python right after `recvmsg()` returns — same convention as every other stream, same clock the frame recorder uses. `kernel_realtime_ns` is kept too, but only as a secondary field, useful later if you ever want to measure exactly how much latency exists between the kernel receiving a frame and Python getting around to reading it.

```python
def _parse_kernel_timestamp(ancdata) -> int | None:
    for level, cmsg_type, cmsg_data in ancdata:
        if cmsg_type == SO_TIMESTAMPNS and len(cmsg_data) >= 16:
            sec, nsec = struct.unpack("=qq", cmsg_data[:16])
            return sec * 1_000_000_000 + nsec
    return None
```

Returns `None` if the option isn't there rather than raising — because a different, fancier timestamping option (`SO_TIMESTAMPING`) was tried on this same hardware and genuinely returned nothing on `vcan0`. A missing kernel timestamp must never crash a recording; it just means `kernel_realtime_ns` is `None` for that frame, which is fine since it's not the field anything depends on.

### `SocketCanSource` — the real reader

```python
def start(self) -> None:
    sock = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    ...
    sock.bind((self.interface,))
```

Three things worth noticing:

1. **It never calls `send()` or `sendto()`, anywhere in this file, on principle.** This sprint is read-only logging on a vehicle's live bus; the code itself is one guarantee, and bringing the interface up in `listen-only` mode at the kernel level (`sudo ip link set can0 type can ... listen-only on`, done once, outside Python, before this ever runs) is a second, independent guarantee — the controller physically can't ACK a frame in that mode, which is a stronger promise than "the code chooses not to."
2. **`can_filters` defaults to `None`**, meaning "accept every ID." This was a deliberate choice, not an oversight — see the docstring, and the memory note on it. The original sprint plan recommended hardware filters for only the already-known IDs, but you asked for it to be easy to add new signals later, and CAN traffic is small compared to video, so capturing everything up front means a new DBC entry is all a new signal ever needs — never a re-drive.
3. **The read loop uses a short timeout, not a blocking read:**

```python
def frames(self) -> Iterator[CanFrame]:
    sock.settimeout(0.2)
    while not self._stop.is_set():
        try:
            raw, ancdata, msg_flags, addr = sock.recvmsg(CAN_FRAME_SIZE, _ANCILLARY_BUFSIZE)
        except socket.timeout:
            continue
        ...
        yield frame
```

Why not just block forever on `recvmsg()`? Because `stop()` just sets a `threading.Event` from another thread — it doesn't interrupt a blocking syscall. Without the timeout, asking the recorder to stop while the bus is quiet (which, on a vehicle, is *normal* — see §3) would hang until a frame happened to arrive. The 0.2s timeout is the mechanism that makes `stop()` actually responsive.

### `FakeCanSource` — testing without a car

Same role as `FakeCameraSource` in Sprint 1: cycles through a fixed list of `(can_id, data)` messages at a controllable rate, with a `realtime` flag to either pace itself to real time (for timing tests) or fire instantly (for fast unit tests). This is what let 23 of the 29 tests run without any CAN hardware at all — only the six tests in `SocketCanSourceRealInterfaceTest` need `vcan0`, and they skip cleanly (not fail) when it's absent.

---

## 3. `can/pipeline.py` — the `CanRecorder`

If you've read `frames/pipeline.py`, most of this will look familiar on purpose — same capture-thread/writer-thread split, same non-blocking queue hand-off, same drop counters, same shutdown sequence. Three real differences:

### No encode stage

`_capture_loop` puts `(seq, frame)` straight onto the queue — no `_prepare()`, no thread pool, no `Future`. A CAN frame is already a complete record; there's nothing between "received" and "ready to write."

### Silence is not a fault

This is the one that actually mattered to get right. `frames/pipeline.py` has a `stall_timeout_s`: no frame for 5 seconds means the camera probably died, so the run ends with a clear reason. **`CanRecorder` has no equivalent at all.** On a vehicle, the bus goes completely silent whenever the ignition is off or the car falls asleep — that's the single most common state a recorder will spend time in, not an error condition. If this pipeline had inherited the frame recorder's stall watchdog unchanged, the very first quiet moment during a real drive would have ended the recording and called it a failure. `run()` just loops `while capture.is_alive() and not self._stop_requested.is_set()` — nothing in there is watching for silence.

### Drop accounting has an extra layer, and one that was deliberately *not* included

Frames only had one drop counter that mattered in practice (`frames_dropped_queue` — the writer/disk falling behind). CAN has that too (`self.dropped`, same meaning, same non-blocking `queue.put_nowait()` pattern), but there's a second, lower-level place a CAN frame can be lost that has no frame-pipeline equivalent: the kernel's own network-interface layer, before your process ever sees the frame at all — exactly the risk the original sprint plan flagged ("SPI CAN controllers have small RX buffers. At high bus load ... frames can overflow"). `read_if_stats()` reads the kernel's per-interface counters directly:

```python
def read_if_stats(interface: str) -> dict | None:
    stats = {}
    for name in _IF_STAT_NAMES:   # rx_packets, rx_dropped, rx_errors, rx_over_errors, rx_fifo_errors
        try:
            with open(f"/sys/class/net/{interface}/statistics/{name}") as f:
                stats[name] = int(f.read().strip())
        except OSError:
            return None
    return stats
```

Called once at the start of `run()` and once at the end; both snapshots go in the manifest (`if_stats_before`, `if_stats_after`). Comparing them after a session tells you whether the *interface* dropped anything, independent of whether this process kept up with what the interface handed it.

There's a third layer, narrower still — this process's own socket receive buffer overflowing between the kernel delivering a frame and Python calling `recvmsg()` — that a socket option called `SO_RXQ_OVFL` is specifically meant to report. **It isn't wired into this code.** I tested it deliberately: shrank a socket's receive buffer to 512 bytes, flooded 500 frames at it before reading anything, confirmed only 3 actually arrived (so the overflow was real), and `SO_RXQ_OVFL`'s ancillary data never showed up at all — on the interface tested (`vcan0`). Rather than ship a mechanism I couldn't verify does anything, I left it out and documented the gap in the code's docstring. It's worth trying again once real CAN hardware is connected, since a real driver might behave differently than the virtual one — but that's a "verify, then maybe add" task, not something to build on faith now.

### Everything else is the same shape as Sprint 1

`_writer_loop` pulls from the queue, writes a CSV row via `DurableCsvWriter` (unchanged from Sprint 1 — literally the same class, same file), ticks the CSV's flush/fsync timers when idle. `_finish()` builds a summary dict, writes it into the session manifest under `session.manifest["streams"]["can"]`, and calls `session.finalize()`. If you understand `frames/pipeline.py`'s `_finish()`, you already understand this one.

---

## 4. `can/record.py` — the CLI

Same shape as `frames/record.py`: parse arguments, build a source, create a `Session`, build a recorder, install signal handlers (`SIGINT`/`SIGTERM` request a clean stop; `SIGHUP` is ignored so a dropped SSH session doesn't kill a running recording), start a status-printing thread, call `.run()`, print the summary.

```
# fake bus, anywhere
python -m ivision.can.record --source fake --duration 10

# real interface, on the Pi (bring the interface up first)
sudo ip link set can0 type can bitrate 500000 listen-only on
sudo ip link set can0 up
python -m ivision.can.record --source socketcan --interface can0 --duration 60
```

It's a completely separate process from `frames.record` — no shared imports beyond `Session` and the standard-library bits both happen to use, no shared state, no coordination at start or stop. That's not an oversight, it's the sprint's requirement (its own entry point, independent of frame capture), and it's also *how* synchronization ends up working: see §5.

---

## 5. Synchronization with the frame stream

This was the part of your original request I want to make sure actually landed, because the answer is easy to miss: **there is no synchronization code**. No shared clock, no offset table, no "align these two logs" step anywhere.

The reason it doesn't need one: `time.monotonic_ns()` reads `CLOCK_MONOTONIC`, and on Linux that's a single clock shared by the whole machine — every process reading it at the same real instant gets (very nearly) the same number, because they're all reading the same kernel counter, not some per-process notion of "time since I started." `frames/pipeline.py` stamps every frame with `time.monotonic_ns()`. `can/pipeline.py` stamps every CAN frame with `time.monotonic_ns()`. Run `frames.record` and `can.record` as two separate commands, at the same time, on the same Pi, during the same drive — even writing to two entirely separate session folders — and their `timestamp_ns` columns are already on one shared timeline. To line up a frame with the CAN traffic at that moment, you just compare the two numbers directly. No conversion, because there was never a second clock to convert from in the first place.

The one thing that has to stay true for this to hold: **both recorders have to run on the same physical machine.** If you ever moved CAN logging to a second device (a different Pi, a laptop), this stops being free and you'd need the realtime-anchor conversion described in `docs/timestamp-convention.md` instead. Not a concern here — this is a single on-vehicle logger — but worth knowing why it's true now, in case the hardware setup ever changes.

---

## 6. Cheat sheet

- `struct can_frame` is 16 bytes: `=IB3x8s`. Verified against a real socket, not just the header file.
- The top 3 bits of `can_id` are flags (extended/RTR/error), not part of the ID — always mask them off before treating `can_id` as the arbitration ID.
- SocketCAN's kernel RX timestamp is on `CLOCK_REALTIME`, not `CLOCK_MONOTONIC` — don't use it as a stream's primary timestamp without converting, and this codebase doesn't (it uses its own `monotonic_ns()` read instead).
- A quiet CAN bus is expected, not a fault — this pipeline has no stall watchdog, unlike the frame pipeline, and that's intentional.
- Three different layers can lose a CAN frame: the interface/driver (caught via `/sys/class/net/.../statistics/`), this process's socket buffer (meant to be caught by `SO_RXQ_OVFL`, but that tested as non-functional on `vcan0` and isn't wired in), and this pipeline's own queue (caught, same pattern as frames' `frames_dropped_queue`). Don't conflate them when debugging a "why did I lose data" question.
- Frame-CAN synchronization needs no code at all as long as both recorders run on the same machine — they already share one `CLOCK_MONOTONIC` timeline by construction.
- `vcan` (virtual CAN) is the way to bench-test any of this without a car or hardware: `sudo modprobe vcan && sudo ip link add dev vcan0 type vcan && sudo ip link set up vcan0`.
