# Sprint 1 walkthrough: frame capture

Written 2026-09-22, when Sprint 1 closed. Goal: you should be able to read this once and then write (or rewrite, or debug) any part of this system yourself, without re-deriving the reasoning from scratch. Where a design choice traces back to a specific failure on real hardware, I've said so — that's usually more convincing than the abstract argument.

Read this in order. Section 2 is the one piece of advice: if you only read one section, read that one, because it's the actual system stripped to its essence.

---

## 1. The big picture

**What it does, in one sentence:** point an IMX219 camera at the world, and every ~33ms, save what it sees as one JPEG file plus one row in a CSV, forever, until told to stop.

**One frame's journey**, matching the real code path:

```
libcamera (kernel/ISP)
    |  cam.capture_request()
    v
capture thread (pipeline.py: _capture_loop)
    |  raw pixel array + SensorTimestamp
    |  hands off via a bounded queue (never blocks)
    v
encoder (inline, or a thread pool)     <- encode.py: JPEG bytes
    v
writer thread (pipeline.py: _writer_loop)
    |  write to frames/00000042_<ts>.jpg.tmp, then rename
    |  append a row to frames.csv
    v
disk
```

**A session on disk:**

```
sessions/<session_id>/
    session.json     manifest: start/stop clock anchors, camera config, per-stream counters, status
    frames.csv        seq, filename, timestamp_ns  -- one row per frame
    frames/            00000000_<timestamp_ns>.jpg, 00000001_<timestamp_ns>.jpg, ...
    system.csv         one row/second: memory, CPU, disk, temperature (if sysmon is on)
```

**Module map.** Everything under `src/ivision/`:

| File | One line |
|---|---|
| `clock.py` | Reads three clocks back-to-back; defines the timestamp convention |
| `session.py` | Creates the session folder, writes the manifest atomically |
| `csvlog.py` | A CSV writer that survives a power cut with bounded loss |
| `frames/source.py` | The `Frame` type and the `FrameSource` interface; a fake camera for testing |
| `frames/camera.py` | The real camera, via Picamera2 |
| `frames/encode.py` | JPEG encoding (simplejpeg/Pillow) and software rotation |
| `frames/pipeline.py` | `Recorder`: the three threads, the queue, drop accounting, shutdown |
| `frames/verify.py` | Checks a finished session for problems |
| `frames/rebuild.py` | Reconstructs `frames.csv` from the frame filenames alone |
| `frames/record.py` | The CLI: wires everything together, one session |
| `frames/cycle.py` | The CLI: record/cool/repeat, several sessions |
| `preflight.py` | Warns about bad conditions *before* recording starts |
| `sysmon.py` | Logs machine health *during* recording, once a second |
| `frames/camera_probe.py`, `camera_experiments.py`, `encode_bench.py`, `disk_bench.py` | One-off diagnostic tools, not part of the recording path itself |

### The dependency graph, and the order to read files in

This is the exact `from .foo import bar` graph — verified by grepping every import in `src/ivision`, not reconstructed from memory. Nothing here is inferred:

```
Layer 0 (nothing internal)     clock.py   csvlog.py   frames/source.py   frames/encode.py   preflight.py
                                   |           |              |                  |
Layer 1                     session.py     sysmon.py     frames/camera.py
                             (needs clock)  (needs csvlog) (needs source, encode)
                                   |
Layer 2                     frames/pipeline.py
                             (needs csvlog, session, source)
                                   |
                          ,--------+--------.
Layer 3          frames/verify.py   frames/rebuild.py
                  (needs session,    (needs pipeline)
                   pipeline)
                                   |
Layer 4                     frames/record.py
                             (needs preflight, session, sysmon, pipeline, source,
                              and camera.py — but only loaded lazily, inside a
                              function, so this file still imports on a machine
                              with no picamera2 installed)
                                   |
Layer 5                     frames/cycle.py
                             (needs record.py only — it has no session-management
                              code of its own; every chunk is just another call
                              into record.record_one())
```

Diagnostics sit outside this graph — they're read separately, only when you need them: `disk_bench.py` has no internal dependencies at all (it never touches the camera), `encode_bench.py` needs `encode.py`, and `camera_probe.py`/`camera_experiments.py` need `camera.py`.

**Read in this order** — each layer only needs the ones before it, so nothing you read requires you to already understand something later:

1. **`clock.py`, `csvlog.py`, `frames/source.py`, `frames/encode.py`** (any order — they don't depend on each other or on anything else). These are the four ideas the whole system is built from: what a timestamp means, how to write a file safely, what a "frame" is, how to turn one into JPEG bytes. Small enough to read in a few minutes each.
2. **`session.py`** — one layer up, now that `clock.py` makes sense. What gets written to disk once per recording, and why the write is atomic.
3. **`frames/camera.py`** — the real camera, now that `encode.py` and `source.py`'s `Frame`/`FrameSource` shape make sense. This is where the Picamera2-specific lessons live.
4. **`frames/pipeline.py`** — the center of the system, and the reason the previous three exist. Don't read this before the others; the queue, the drop accounting, and the shutdown sequence only make sense once you already know what a `Frame` is, what a `Session` writes, and how `csvlog.py` behaves under a crash.
5. **`frames/verify.py`, `frames/rebuild.py`** — both are just readers of what `pipeline.py` already defined (`COLUMNS`, `parse_frame_filename`). Read them together; `rebuild.py` is the answer to "what if the thing `verify.py` checks for is missing."
6. **`frames/record.py`** — the payoff. By this point every import at the top of the file is something you've already read; this is just wiring.
7. **`frames/cycle.py`** — quick, once `record.py` makes sense: it's a loop around `record.record_one()`, nothing more.
8. **`sysmon.py`, `preflight.py`** — can genuinely be read any time (§1 already covers what they're for); grouped here because seeing them right after `record.py` shows exactly where they plug in (`record_one()` calls both).
9. **The diagnostic tools**, only when you need one of them specifically — they don't teach you the system, they answer one narrow question each (§7).

If you only have twenty minutes: read layers 0-2 (`clock.py` through `pipeline.py`) and skip straight to §2 below, which is the same material taught the other direction — smallest possible working version first, then what breaks.

That's the whole system. Now the part that actually teaches you something.

---

## 2. If you were writing this yourself

Strip away every piece of hardening and this is what's left — genuinely all you need to capture frames from this camera. This is close to the first thing I'd write by hand, and close to what you could type out in an evening:

```python
#!/usr/bin/env python3
"""The essence of Sprint 1. No hardening. Do not use this for a real
recording -- see record.py. This is here so you can see what the real
system actually adds, and why."""

import csv
from pathlib import Path

from picamera2 import Picamera2
import simplejpeg

WIDTH, HEIGHT, FPS, QUALITY = 1280, 960, 30, 75
OUT = Path("session")
(OUT / "frames").mkdir(parents=True, exist_ok=True)

cam = Picamera2()
frame_us = round(1_000_000 / FPS)
cam.configure(cam.create_video_configuration(
    main={"size": (WIDTH, HEIGHT), "format": "RGB888"},
    controls={"FrameDurationLimits": (frame_us, frame_us)},
))
cam.start()

with open(OUT / "frames.csv", "w", newline="") as f:
    writer = csv.writer(f)
    writer.writerow(["seq", "filename", "timestamp_ns"])
    seq = 0
    try:
        while True:
            request = cam.capture_request()
            try:
                timestamp_ns = request.get_metadata()["SensorTimestamp"]
                pixels = request.make_array("main")
            finally:
                request.release()  # give the buffer back to libcamera at once

            jpeg_bytes = simplejpeg.encode_jpeg(pixels, quality=QUALITY, colorspace="BGR")
            filename = f"frames/{seq:08d}_{timestamp_ns}.jpg"
            (OUT / filename).write_bytes(jpeg_bytes)
            writer.writerow([seq, filename, timestamp_ns])
            f.flush()
            seq += 1
    except KeyboardInterrupt:
        pass

cam.stop()
cam.close()
```

That's about 40 lines, and it works — it'll capture real frames. Every design decision in the actual codebase is either **in this snippet already** (fixed frame duration, `release()` immediately, filename carries the timestamp) or **exists to fix a specific way this snippet breaks**:

| This snippet... | ...breaks like this | ...which is why the real code has |
|---|---|---|
| Encodes JPEG in the same loop that reads the camera | If encoding is slow, the next `capture_request()` is late. On the Pi 3B, JPEG encoding *is* slow enough to matter | A **bounded queue** between capture and encode/write, so a slow encoder delays writes, never the sensor read |
| Writes the JPEG directly to its final name | A power cut mid-write leaves a truncated file with a valid-looking name | Write to `.tmp`, then `os.replace()` — atomic, so a reader never sees a half-written file |
| `f.flush()` every row, no `fsync()` | Looks safe; isn't. `flush()` only leaves Python — the OS can still lose it in a crash. But calling `fsync()` every row was tried and made things *worse* (see §5) | A deliberate, tuned split: buffer + periodic flush + separate, rarer `fsync()` |
| No error handling at all | One bad frame, one full disk, one camera hiccup — and the whole process dies with a traceback, mid-write, no record of why | Every failure mode is caught, counted, and the session still closes cleanly with a reason |
| No way to know how many frames were *dropped* | Silent data loss. You'd never know your 10-minute session actually only captured 8 | `frames_captured`, `frames_written`, and two separate drop counters, cross-checked by `verify.py` |
| Stops only on Ctrl-C | If the camera itself hangs (not the process — the camera driver), `capture_request()` never returns and nothing happens | A stall watchdog: no frame for 5 seconds ends the run with a clear reason |
| No idea if the camera and the CSV agree on what "now" means | Fine until you add a second sensor (CAN) with its own clock | The monotonic-clock convention (§3) |

If you can hold that table in your head, you understand the *shape* of the real system. Everything below is detail.

---

## 3. The core pipeline

### `clock.py` — what "timestamp" means

The Pi has no RTC (real-time clock chip), so wall-clock time is untrustworthy until NTP corrects it, and can jump backward or forward when that happens. So every timestamp in this whole project is **integer nanoseconds on `CLOCK_MONOTONIC`** — a clock that only ever counts up, with no defined relationship to wall time. `read_anchors()` records four numbers once per session (`monotonic_ns`, `boottime_ns`, `realtime_ns`, `read_span_ns`), which is what lets a *reader* convert back to UTC later, without the recorder ever having to trust wall-clock time while it's running. Read `docs/timestamp-convention.md` for the full reasoning — this decision is the one thing every later stream (CAN, GPS, IMU) has to follow too.

### `session.py` — the manifest

`Session.create()` makes the folder and writes `session.json` with `status: "recording"`. `finalize()` (called at the very end) rewrites it with `status: "complete"`. **That one field is the crash-detection mechanism**: if a session's manifest still says `"recording"`, you know it ended badly, without needing anything else.

The write itself (`flush()`) is atomic: write to `session.json.tmp`, `fsync()` it, then `os.replace()` over the real name. A reader either sees the complete old manifest or the complete new one, never a half-written one — `os.replace()` is a single filesystem operation, not two.

### `csvlog.py` — the CSV writer, and why it's not simpler

This is used for `frames.csv` and `system.csv` both. The policy, stated in its docstring: *an unclean power cut loses at most the last few rows, and never corrupts earlier ones.* Three mechanisms combine to do that:

1. Rows are buffered in memory, and only written with `os.write()` calls of **whole formatted lines** — never a partial row.
2. Buffered rows are flushed to the OS every `flush_interval_s` (default 0.5s).
3. The file is `fsync()`'d — forced out of the OS's page cache onto the actual disk — every `fsync_interval_s` (default 1.0s, but see §5 for why the real recorder overrides this to a much larger number).

Why not just `fsync()` every row, for maximum safety? Because on this hardware, `fsync()` is expensive — it forces the filesystem's journal to commit, which on a slow SD card can **stall the whole write path for seconds**. That's a real, measured failure (§5), not a hypothetical one. The two-tier design (flush often, fsync rarely) is a deliberate trade: you can lose up to `fsync_interval_s` worth of rows in a genuine power cut, but you don't get multi-second stalls during normal operation.

### `frames/source.py` — the interface

`Frame` is a small frozen dataclass: `seq`, `timestamp_ns`, `data`, `ext`. `data` is either finished JPEG bytes (the fake source, for testing) or a raw pixel array that still needs encoding (the real camera). `FrameSource` is the protocol every camera-like thing implements: `start()`, `stop()`, `frames()` (a generator), `close()`, `describe()`, and *optionally* `encode(frame)`. The recorder checks for `encode` with `getattr(source, "encode", None)` — if it's there, encoding happens outside the capture loop; if not, `frame.data` is assumed to already be final bytes.

This is the seam that let almost everything else in this project be tested without a camera: `FakeCameraSource` implements the same interface, with a `realtime` flag that either paces itself to real wall-clock time (for timing-sensitive tests) or fires instantly (for fast unit tests).

### `frames/pipeline.py` — the `Recorder`

This is the one file worth understanding line-by-line, because everything else calls into it. Three threads:

- **capture thread** (`_capture_loop`): calls `source.frames()` and, for each frame, does a **non-blocking** `queue.put_nowait()`. If the queue is full, the frame is dropped and counted — never blocked on. This is the single most important design rule in the file: *the sensor read must never wait on the disk.*
- **encoder** (optional): either runs inline inside the capture thread (`encode_workers=0`), or the capture thread submits encoding jobs to a `ThreadPoolExecutor` and puts the resulting `Future` on the queue instead of a `Frame`. Either way, a `BoundedSemaphore` (`max_inflight`) caps how many raw frames can be mid-encode at once, which bounds memory.
- **writer thread** (`_writer_loop`): pulls items off the queue, resolves a `Future` if that's what it got, writes the file (`.tmp` then rename), then writes the CSV row. Only after the file exists does the row get written — a row never points at a missing file.

**Drop accounting is exact, and `verify.py` checks the arithmetic**: every frame the capture thread sees ends up in exactly one bucket — written, dropped-by-queue, dropped-by-encoder, or unwritten-after-error. `frames_captured == len(ledger rows) + dropped_queue + dropped_encode + unwritten_after_error`, always. This is what let real bugs get caught (§5) instead of just quietly losing frames.

**Shutdown** is the part most likely to bite you if you modify this file. The sequence in `run()`:
1. Signal the source to stop.
2. Wait up to `stop_timeout_s` for the capture thread to actually exit.
3. **Always** try to close the source now — even if the capture thread is still stuck (it can be, if it's blocked inside a C call that will never return). Skipping this once leaked the Pi's camera memory pool until a reboot fixed it (§5) — that bug is exactly why this is unconditional now, not in an `else` branch.
4. Push a sentinel onto the queue so the writer thread's `get()` unblocks and it can exit.
5. Join the writer, shut down the encoder pool.

### `frames/camera.py` and `frames/encode.py` — the real camera

The Picamera2-specific gotchas, learned by actually running on hardware, not from documentation:

- `cam.sensor_modes` **reconfigures the camera as a side effect** to find out what modes exist. Read it once, before `configure()`/`start()`, and cache it — calling it again after `start()` raises `"Camera must be stopped before configuring"`.
- `FrameDurationLimits` set to `(d, d)` (same min and max) makes the sensor free-run at a fixed rate — the cadence comes from the sensor, not a software timer.
- `request.make_array("main")` returns a **copy**, so it's safe to use after `request.release()`. Call `release()` immediately, in a `finally`, so libcamera gets its buffer back as fast as possible (this matters a lot when buffers are scarce — see CMA in §5).
- The timestamp is `request.get_metadata()["SensorTimestamp"]`, and it was verified (not assumed) to be on the `CLOCK_MONOTONIC` timeline — measured by comparing it against the session's start anchor and confirming the offset matched the real camera startup time.
- Encoding: `simplejpeg` (a thin wrapper on libjpeg-turbo) is the default, and it's not a small win — measured at 18/36/67 fps across 1/2/4 threads on the Pi 3B, versus Pillow's 10/13/14 (Pillow's threads barely help at all). `pick_encoder()` falls back to Pillow automatically if `simplejpeg` isn't installed.
- `rotate_cw()` exists and is tested, but **do not use `--rotate` on the Pi 3B** — a 90°/270° rotation forces a cache-hostile memory copy that costs more than the JPEG encode itself (measured: 130ms vs 41ms). Rotation is deferred to Phase 2 instead. Full story in `docs/capture-performance.md`.

---

## 4. `verify.py` — how you know a session is actually good

Three kinds of checks, in order of how bad it is if they fail:

1. **Structural** (errors, fail the check): does every ledger row have a matching file, does every filename's embedded seq/timestamp match its own row, are sequence numbers and timestamps strictly increasing, does the manifest's frame count reconcile against the ledger. These should never happen on a clean run.
2. **Expected-after-a-crash** (warnings, don't fail): a torn final CSV row, a few trailing files with no ledger row yet — normal if the manifest still says `"recording"`, an error if it says `"complete"`.
3. **Data-quality** (warnings): a stall (a gap over 1 second between frames), or any nonzero drop count. These don't mean the session is broken — drops are counted, not silent — but they're worth seeing.

It also computes a frame-interval histogram and an `achieved_fps`, which is how every performance number in `docs/capture-performance.md` was actually measured.

---

## 5. The hardening — and the failure that justified each piece

This is the part that's easy to skip and expensive to skip. Every one of these exists because of something that actually happened on real hardware this sprint, not because it seemed prudent in the abstract.

- **Stall watchdog** (`pipeline.py`, `stall_timeout_s`). Early on, the camera would occasionally stop delivering frames entirely (`Camera frontend has timed out`, a libcamera/ISP-level failure). Without a watchdog, the process just sat there forever. Now: no frame for 5 seconds ends the run with a specific reason.
- **Always-close-on-shutdown** (`pipeline.py`, discussed above). A stalled capture thread used to skip closing the camera. That leaked CMA — a small, fixed 256MB memory pool the Pi reserves for camera/GPU DMA buffers, separate from ordinary RAM. Two stalls in a row left under 1MB free, and every later camera start failed with `Cannot allocate memory` until the Pi was rebooted. The fix (always attempt `close()`, even racing a stuck thread) is a genuine "the alternative is worse" trade-off — read the comment in the code, it explains why.
- **`--csv-fsync-interval`, defaulting away from 1.0 in practice.** The very first attempt to fsync the ledger every second caused multi-second write stalls on the SD card, because each `fsync()` forces an ext4 journal commit, and that commit has to flush pending frame-file writes first. Measured: `fsync`'d writes sustain about 6.4MB/s on this card; buffered writes look fine for 15-20 seconds then stall for several more while the kernel catches up. `--csv-fsync-interval 3600` (in practice, "let the kernel's own periodic write-back handle it") fixed this.
- **Filenames carry their own timestamp, and `rebuild.py` exists.** Once the ledger stopped being fsync'd every row, a power cut could lose recent ledger rows outright. The fix: put the timestamp in the filename too (`<seq>_<timestamp_ns>.jpg`), so the ledger is redundant with the files. `rebuild.py` reconstructs `frames.csv` from the frames directory alone, reading only what's already in each filename — it never invents a timestamp.
- **`preflight.py`.** A whole day of recordings failed in different ways — disk stalls, camera stalls, a full hang — and the actual cause, most of the time, wasn't the code at all: VS Code Server's Remote-SSH connection was using ~480MB of the Pi's 905MB, leaving so little free RAM that the kernel's disk write-back cushion shrank to almost nothing. `preflight` checks memory, swap, temperature, the under-voltage alarm, and disk space *before* the first frame, and — usefully — names the biggest memory users so "close VS Code" isn't a guess.
- **`sysmon.py`.** Before this existed, a failure gave you a stack trace and nothing else — no way to know if it was heat, memory, or disk. Once it existed, every subsequent failure could be diagnosed from data instead of guesswork: `system.csv` is what actually proved the 1640x1232 failures were thermal throttling (CPU jumping from 55% to 80%+ for the same work, temperature crossing 85°C), not something else.
- **`encode_nice`.** Encoder threads run at a lower OS priority than the capture thread, so if they fall behind, *they* lose the CPU contention, not the camera. Without this, heavy encoding once starved libcamera badly enough that the camera itself stalled.
- **`SIGHUP` handling** (`record.py`). If you start a recording over SSH and the connection later drops (e.g. leaving Wi-Fi range), Python's default reaction to the resulting `SIGHUP` is to terminate the whole process. `record_one()` now ignores it. (This alone isn't sufficient if you leave the process's stdout attached to the dead SSH pipe — see the comment in the code for why `nohup ... > file 2>&1 &` still matters.)

None of this was designed in advance. It was all added in response to a specific, reproduced failure. If you're extending this system and tempted to add a new piece of hardening "just in case," that's a sign to first go reproduce the failure you're worried about — it'll tell you whether your fix actually addresses it.

---

## 6. The two recording modes

**`record.py`** is the CLI that ties everything above together for one session: parse arguments, build a source, create a session, build a `Recorder`, install signal handlers, start `sysmon`, run, print a summary. `record_one()` is the reusable core — it's what `cycle.py` calls too.

**`cycle.py`** exists because sustained JPEG encoding heats the Pi 3B to its throttle cap within a few minutes at some resolutions. Rather than fight that in software, `cycle.py` records for N minutes, sleeps (camera fully closed — cools genuinely, and the next chunk starts from a clean camera state) for M minutes, and repeats until a target *recorded* duration is reached. Each chunk is an entirely ordinary session — `cycle.py` adds no new session format, it just calls `record_one()` in a loop with a shared `source` object and a shared parsed `args`. That reuse (`record.add_camera_args`, `record.add_recorder_args`, `record.build_source`) is why the two CLIs share every option and behave identically per-chunk.

---

## 7. The diagnostic tools

These aren't part of the recording path — they're the tools that answered specific questions during this sprint, kept because the same questions will come up again on different hardware or settings.

| Tool | Question it answers |
|---|---|
| `camera_probe.py` | Does the camera work at all, what sensor modes exist, is the timestamp really on the monotonic clock, roughly how fast can one thread encode |
| `camera_experiments.py` | Of N things that differ between a known-good baseline (`rpicam-hello`) and this code, which one actually causes a given failure — runs each variant in its own process |
| `encode_bench.py` | Which JPEG encoder, and how many threads, for a given resolution — measured on one real captured frame, not a synthetic one |
| `disk_bench.py` | Can this disk (SD card, USB drive, whatever) sustain the write rate a recording needs — paces writes without touching the camera at all, so disk and camera problems can't be confused with each other |

The pattern worth noticing across all four: **when something breaks and the cause isn't obvious, build the smallest possible tool that isolates just that one variable**, rather than staring at the full system. Every real bug found this sprint (the disk stall, the CMA leak, the thermal throttling, the rotation cost) was diagnosed this way.

---

## 8. Cheat sheet: what will bite you if you build this again

- The Pi 3B throttles at **85°C**. A heatsink helps but doesn't prevent it at high resolutions with 3 encoder threads; it does at 1280x960 with 2.
- **CMA** (camera DMA memory, 256MB fixed) is a separate pool from ordinary RAM. `free -m` won't show you it's exhausted; `grep Cma /proc/meminfo` will.
- `cam.sensor_modes` must be read **before** `start()`, not after.
- `fsync()` is expensive on a slow SD card — expensive enough to stall a whole pipeline for seconds. Don't call it per-row on a hot path; batch it.
- **simplejpeg beats Pillow by 3-5x** on this hardware. Always benchmark the actual libraries on the actual device — the "obvious" choice (Pillow, since everyone's used it) was 3x too slow to notice.
- **Software 90°/270° rotation is expensive** on weak ARM hardware: a transpose forces a cache-hostile memory copy that costs more than JPEG encoding itself. 180° (a flip, not a transpose) wasn't measured — don't assume it's cheap either.
- **VS Code Server's Remote-SSH connection uses real memory on the remote end** — close it before a resource-constrained recording.
- **A recording started over SSH dies when the connection drops**, unless you both ignore `SIGHUP` *and* detach stdout from the SSH pipe (`nohup ... > file &`).
- When a failure is mysterious, **measure before theorizing.** Every real cause this sprint (memory, disk, heat, a hardware limitation) was found by adding instrumentation and reading the data, not by reasoning about what seemed likely.
