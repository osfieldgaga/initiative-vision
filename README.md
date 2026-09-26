# Initiative Vision 1: on-vehicle data acquisition

Phase 1 logs camera frames and CAN bus data with timestamps on a Raspberry Pi 3B, on the vehicle. Plans are in `claude/sprints/`, starting with `00-roadmap.md`.

## Quickstart: record frames + CAN together (on the Pi)

Standing config, always used: **1280×1280, quality 75, 3 encoder threads**. Two commands, in two terminals — frame capture creates the session, CAN joins it, so both land in one folder with one manifest.

**Terminal 1** — start frame capture, note the `session:` path it prints:
```
cd ~/Desktop/initiative-vision
PYTHONPATH=src python3 -m ivision.frames.record --source picamera2 \
  --width 1280 --height 1280 --quality 75 --encode-workers 3 \
  --out sessions --status-every 10
```

**Terminal 2** — join that session with CAN (check `can0` is listen-only first: `ip -details link show can0` should show `LISTEN-ONLY` — it does not survive a reboot, reset it if needed with `sudo ip link set can0 down && sudo ip link set can0 type can bitrate 500000 listen-only on && sudo ip link set can0 up`):
```
cd ~/Desktop/initiative-vision
PYTHONPATH=src python3 -m ivision.can.record --source socketcan --interface can0 \
  --session sessions/<paste the path Terminal 1 printed> --status-every 10
```

Drive, then **Ctrl-C both** when done — each finishes and finalizes its part of the shared manifest independently, in whatever order you actually stop them.

Frames-only or CAN-only work the same way without `--session` (see their own sections below). To decode a session's raw CAN traffic against the Malibu's DBC afterward, see "Decode CAN signals against the Malibu's DBC" below.

## Current status (2026-09-26)

Frame capture and CAN logging are both proven on real hardware, including together against the live vehicle bus. **Standing settings for frames**, in use always (full measurement story in `docs/capture-performance.md`):

```
PYTHONPATH=src python3 -m ivision.frames.record --source picamera2 --width 1280 --height 1280 --quality 75 --encode-workers 3
```

Worth knowing honestly, not glossing over: this is denser than the doc's zero-drop-proven config (1280×960, 2 threads, ~7.2GB/hour) — at 1280×1280/3 threads, a real 10-minute session measured ~0.64% dropped frames and a peak temperature of 86°C (just over the 85°C throttle cap), versus zero drops and 78.4°C at 1280×960/2 threads. It's the standing choice anyway; 1280×960/2 threads remains the fallback if thermal reliability or drops become a real problem. Higher resolutions still (1640x1232, 1080p) hit thermal throttling and dropped far more frames on this Pi 3B even with a heatsink fitted — see the doc for what was tried and why it was rejected.

CAN logging is proven against the real Malibu bus in listen-only mode: a 90-second real capture sustained ~2,280 msg/s with zero drops (queue, and confirmed against kernel interface counters). The Malibu's DBC (`dbc/gm_global_a_powertrain.dbc`) decodes 29 distinct message types from real traffic, including wheel speed, steering angle, yaw rate, lateral acceleration, and OnStar GPS — see `src/ivision/can/decode.py`.

Also see `docs/timestamp-convention.md` (needs sign-off) for the timing rules every later stream must follow, and `docs/walkthrough/sprint-1-frames.md` for a full explanation of how the code works, including a minimal ~40-line reference version and why every piece of hardening beyond it exists.

## What works today

| Status | What |
|---|---|
| Tested on real hardware | Frame recording (`record.py`), cool-down cycling (`cycle.py`), `verify_session`, `rebuild.py`, encoder/disk/thermal benchmarking tools, `preflight`; CAN recording (`can/record.py`) and live bus reading (`can/dump.py`) against the real Malibu bus in listen-only mode; frames+CAN sharing one session (`Session.attach()`); DBC decoding (`can/decode.py`) |
| Tested here (desktop) | The fake-camera and fake-CAN pipelines, JPEG encoding, unit tests (260+) |
| Doesn't exist yet | GPS/IMU as their own streams (both fold into CAN decoding instead, since OnStar GPS and the car's accelerometer are both CAN signals — see `claude/sprints/sprint-4-gps-optional.md`/`sprint-5-imu-optional.md`), device-side S3 upload (Sprint 6), `can/verify.py` (structural checks on a CAN log, mirroring `frames/verify.py`), a real power-cut test on the Pi itself |

## Requirements

- **Fake camera, verifier, tests on the desktop:** Python 3.9+ and the standard library. The camera-related tests also need `numpy` and `Pillow`. Tested on Python 3.13.
- **Real camera, on the Pi:** Raspberry Pi OS with the libcamera stack (Bullseye or newer), `python3-picamera2` (which brings numpy and Pillow), and the system `python3`. The Pi's OS and Python versions haven't been checked yet.

## Where to run things

- **Tests: on either machine.** They only use temporary directories.
- **The recorder and verifier: on the Pi**, so the files land on the Pi's own storage. The project folder on your desktop is an sshfs mount of the Pi (`/home/osfield/osfield@creampie/Desktop/initiative-vision` here is `/home/osfield/Desktop/initiative-vision` on the Pi). If you run the recorder from the desktop with the default `--out sessions`, every frame is written over the network to the Pi, so the speed and results mean nothing. Use `--out` with a local path on the desktop if you only want a demo.

## Run the tests

From the project folder:

```
PYTHONPATH=src python3 -m unittest discover -s tests
```

Add `-v` to list each test. Expected: `Ran 160 tests ... OK` (`OK (skipped=5)` if simplejpeg isn't installed on that machine).

Optional: prefix `PYTHONDONTWRITEBYTECODE=1` to stop Python leaving `__pycache__` folders in the project.

## Record a demo session (fake camera)

```
PYTHONPATH=src python3 -m ivision.frames.record --duration 5
```

It prints the session directory, records for 5 seconds, then prints a summary. Leave out `--duration` to run until you press Ctrl-C, which finishes the frame in flight and closes the session cleanly.

Options:

| Option | Default | Meaning |
|---|---|---|
| `--out DIR` | `sessions` | Folder that holds sessions. One new sub-folder per run |
| `--duration S` | until Ctrl-C | Stop after S seconds |
| `--fps N` | 30 | Frame rate of the fake camera |
| `--queue-size N` | 256 | Frames buffered between capture and disk. Full queue means frames are dropped and counted |
| `--fsync-files` | off | Force every frame file to disk (slower, safer against power loss) |

## Run with the real camera (on the Pi)

Do this **on the Pi** (SSH in, or use its keyboard), from the project folder, `/home/osfield/Desktop/initiative-vision`.

**0. Is the camera there and is the library installed?**

```
rpicam-hello --list-cameras      # older OS images: libcamera-hello --list-cameras
python3 -c "import picamera2; print('picamera2 ok')"
```

If the first shows no camera, reseat the ribbon (contacts face the right way, latch closed) and reboot. If the second fails: `sudo apt install -y python3-picamera2`. A virtualenv must be created with `--system-site-packages` or it won't see picamera2.

**1. Before recording, check the environment.** Memory pressure, heat and stray VS Code Server connections caused most of the early failures here (full story in `docs/capture-performance.md`). `record` runs this automatically and prints warnings, but you can check by hand too:

```
vcgencmd measure_temp; vcgencmd get_throttled
pgrep -af "[.]vscode-server"     # if this prints anything, close VS Code's connection to the Pi first
```

**2. Record.** The standing settings (see "Current status" above for the honest drop-rate/thermal trade-off versus the zero-drop 1280×960/2-thread alternative):

```
PYTHONPATH=src python3 -m ivision.frames.record --source picamera2 --width 1280 --height 1280 --quality 75 --encode-workers 3 --duration 600
```

Watch the status line: `fps` near 30 and both `dropped:` counters at 0 is a pass. `backlog=N/256` climbing means the disk is falling behind.

**3. Verify it:**

```
PYTHONPATH=src python3 -m ivision.frames.verify sessions/<session_folder>
```

Want: `PASS`, `achieved_fps` near 30, no WARNING lines.

**For a long or unattended recording** (in the car, or left running after you disconnect), see "Recording in cool-down cycles" and "Starting a recording that survives losing SSH" below.

Camera options for `record`:

| Option | Default | Meaning |
|---|---|---|
| `--width`, `--height` | 1640 x 1232 | Output size. **Standing choice: `--width 1280 --height 1280`.** `--width 1280 --height 960` is the only setting proven to run with zero dropped frames, if reliability matters more than resolution on a given run; higher resolutions still (1640x1232, 1080p) throttle the Pi 3B even with a heatsink (see `docs/capture-performance.md`). `camera_probe` prints the real sensor mode list |
| `--sensor-mode N` | none | Pin the sensor mode by its index in the probe list. **Use this for any run you'll calibrate against**, so the mode can't change between runs |
| `--quality` | 90 | JPEG quality. Use 75 — see the doc for the full size/quality table |
| `--encode-workers` | 3 | JPEG encoder threads. **Use 3** at the standing 1280×1280 resolution; 2 at 1280×960; 0 = encode in the capture thread |
| `--encoder` | auto | `auto` = simplejpeg if installed (much faster than Pillow on the Pi), else Pillow |
| `--exposure-us`, `--gain` | auto | Fix exposure time and/or analogue gain (turns auto-exposure off). Not yet tested in daylight/moving conditions — see the doc's "not yet measured" list |
| `--status-every S` | 5 | Seconds between status lines, 0 = off |
| `--encode-nice N` | 10 | Nice value for encoder threads, so the camera wins CPU contention (0 = off) |
| `--camera-queue` | default | `off`: Picamera2 recycles old frames itself so a slow consumer cannot starve the camera |
| `--csv-fsync-interval S` | 1.0 | Seconds between fsyncs of the frame ledger. On the SD card, 1.0 causes disk stalls; **use 3600** to avoid them. Trade-off: a power cut can then lose up to ~30 s of ledger rows — but not the frames themselves, since each frame's filename carries its own timestamp; see `rebuild.py` below |
| `--no-preflight` | off | Skip the pre-recording check of memory, temperature, power and disk space |
| `--no-sysmon` | off | Do not write `system.csv` (memory/CPU/disk/temperature, once a second) |
| `--stall-timeout S` | 5 | End the run if no frame arrives for S seconds, 0 = off |
| `--queue-size N` | 256 | Frames buffered between capture and disk before frames start being dropped |

## Recording in cool-down cycles

For a longer recording than has been tested continuously, or as a safety margin at a heavier resolution: `cycle.py` records for a fixed number of minutes, pauses to let the SoC cool, and repeats until a total *recorded* duration is reached. Each chunk is its own ordinary session (its own `frames.csv`, `system.csv`, `verify`-able independently) inside one parent folder — there's a gap in coverage during each cooldown, which is the accepted trade for staying under the thermal limit.

```
PYTHONPATH=src python3 -m ivision.frames.cycle --source picamera2 --width 1280 --height 1280 --quality 75 --encode-workers 3 --record-minutes 5 --cool-minutes 2 --total-minutes 20
```

Tested for real (2026-09-22): 4× 5-minute chunks, 3× 2-minute cooldowns, 26 minutes wall time — all four chunks zero drops, each starting from nearly the same temperature regardless of how hot the previous one got. It takes every option `record` does (same table as above).

## Starting a recording that survives losing SSH

If you start a recording over SSH and then drive away from Wi-Fi range, the SSH session dies. Two separate things can kill the recording when that happens:

1. **SIGHUP.** Python's default reaction to a lost controlling session is to terminate. Fixed (2026-09-22): the recorder now ignores SIGHUP, so losing the connection alone won't stop it.
2. **A broken output pipe.** If the process is still printing its status line to the now-dead SSH connection, that write can itself fail. Ignoring SIGHUP doesn't fix this — you need to detach the process's output from the SSH session:

```
nohup env PYTHONPATH=src python3 -m ivision.frames.record --source picamera2 --width 1280 --height 1280 \
    --quality 75 --encode-workers 3 --duration 1200 > ~/record.log 2>&1 &
disown
```

Reconnect later (once back in Wi-Fi range) and check `~/record.log`, or just look for the session under `sessions/`.

## Recovering the ledger if it's lost

Since 2026-09-22, every frame's filename embeds its own timestamp (`<seq>_<timestamp_ns>.<ext>`), so `frames.csv` is redundant with the files. If it's ever lost, badly torn, or (with a long `--csv-fsync-interval`) simply never flushed before a power cut:

```
PYTHONPATH=src python3 -m ivision.frames.rebuild sessions/<session_folder>              # report only
PYTHONPATH=src python3 -m ivision.frames.rebuild sessions/<session_folder> --replace     # fix it (backs up the old one to frames.csv.orig)
```

This never invents a timestamp — it only reads what's already in each filename.

## Diagnostic tools

Built while tracking down the issues above; the full story behind each is in `docs/capture-performance.md`.

| Tool | What it does |
|---|---|
| `camera_probe` | No recording. Lists sensor modes, measures real frame rate/timing, checks the camera's timestamp clock, times JPEG encoding, saves sample frames. `PYTHONPATH=src python3 -m ivision.frames.camera_probe --frames 90 --save-samples ~/probe_samples` |
| `camera_experiments` | Tries several Picamera2 configurations (format, buffer count, threading) each in its own process, to isolate what makes the camera stall. `PYTHONPATH=src python3 -m ivision.frames.camera_experiments` |
| `encode_bench` | Times JPEG encoders (Pillow, simplejpeg, OpenCV) on one real frame, single- and multi-threaded. `PYTHONPATH=src python3 -m ivision.frames.encode_bench` |
| `disk_bench` | Writes frame-sized files at 30/s without a camera, to compare write strategies and find disk stalls. Point it at a USB drive with `--dir /media/osfield/<NAME>/_disk_bench` to test it as an alternative to the SD card |
| `preflight` (used automatically by `record`) | Warns about low memory, swap, heat, power and disk space before recording |

**If the camera won't start or drops almost all frames**, that message comes from libcamera itself, below this code. Check hardware first, independent of any of this: `rpicam-hello -t 15000 --nopreview --width 1280 --height 960 --framerate 30`, `vcgencmd get_throttled`, `sudo dmesg | grep -iE "unicam|imx219|under-voltage|i2c"`.

## Check a session

```
PYTHONPATH=src python3 -m ivision.frames.verify sessions/<session_folder>
```

It prints stats, then `PASS` or `FAIL`. Exit code 0 means pass, 1 means fail. Add `--no-content` to skip the JPEG start/end marker check on every file.

- **ERROR** lines are things a clean run must never produce (missing file, timestamps going backwards, counts that don't add up).
- **WARNING** lines are expected after an unclean stop, like a power cut: a torn last row, or a few frame files with no ledger row yet. They don't fail the check.

Useful stats in the output: `achieved_fps`, `interval_ms` (mean, p50, p99, max), `interval_histogram`, `seq_gaps` (dropped frames in the middle of the run), `missing_seq_total` (all dropped frames), and `suspected_missing_from_timing` (frames lost before the app saw them, estimated from timing gaps).

**For a CAN session:**

```
PYTHONPATH=src python3 -m ivision.can.verify sessions/<session_folder>
```

Same PASS/FAIL and ERROR/WARNING shape, but **a long gap between frames is never flagged** — the bus going quiet (ignition off, car asleep) is normal, not a stall, unlike the camera. Checks three genuinely different kinds of loss (a real drive found all three happening at once — see "Current status" above): this pipeline's own queue drops, kernel/interface-level drops (`if_stats` in the manifest), and untracked socket-buffer loss (frames the interface delivered that never reached this process, not explained by either of the other two — the `SO_RXQ_OVFL` mechanism that would normally report this doesn't work here, see `can/pipeline.py`'s docstring).

## Decode CAN signals against the Malibu's DBC

`can.csv` is deliberately raw only (arbitration ID + raw bytes, no decoding) — same "raw-first" principle as the frame capture (raw JPEGs, no on-device inference). Decoding is a separate, offline, rerunnable step:

```
PYTHONPATH=src python3 -m ivision.can.decode sessions/<session_folder> --dbc dbc/gm_global_a_powertrain.dbc
```

This needs `cantools`, which nothing else in this project depends on (`pyproject.toml`'s `[decode]` extra, or plain `pip install cantools`). **If you're running this from a different machine than the one that recorded the session** (e.g. a laptop, over the sshfs-mounted project folder) — create the venv *outside* the mounted directory, not inside it:

```
python3 -m venv ~/.venv-ivision-decode          # NOT inside the mounted project folder
~/.venv-ivision-decode/bin/pip install cantools
PYTHONPATH=src ~/.venv-ivision-decode/bin/python3 -m ivision.can.decode sessions/<session_folder> --dbc dbc/gm_global_a_powertrain.dbc
```

A venv created inside an sshfs mount fails outright (`Operation not permitted` on its own interpreter binary — FUSE-based network mounts don't reliably support what `venv` needs to do), and a venv created on one machine can't be reused from another even over a working mount either way (`pip`/`python3` inside a venv are scripts with an absolute path to a specific interpreter baked in at creation time, so they simply don't exist as such from a different machine's point of view). The venv itself just needs to live somewhere local; the project files it operates on are fine to read over the mount.

Output: **one combined `sessions/<id>/decoded.csv`**, alongside `frames.csv`/`can.csv`/`system.csv` — not a per-message-type file. Columns: `timestamp_ns, seq, message`, then every signal from every decoded message, namespaced `<MessageName>.<SignalName>` (e.g. `PSCMSteeringAngle.SteeringWheelAngle`) since several GM messages reuse the same signal name (`RollingCounter` appears on at least three unrelated messages in this DBC) — a bare name would silently conflate different signals.

**Each row is a full snapshot, not just what changed.** Every column holds the most recently decoded value for that signal as of that row's timestamp — forward-filled from whichever earlier row last updated it, not just the columns belonging to that row's own `message`. Row N reads like a dashboard: the complete, best-known state of every signal at that instant. A column stays blank only until that signal has been decoded at least once; the `message` column still says which signal(s) this exact row just updated. One honest consequence: a slow-updating signal (e.g. once a second) shows the same carried-forward value across many faster-updating rows in between — that's what "most recently known" means, not a bug. Rows whose ID has no match in the DBC are counted and printed in the decode summary, not written as rows at all.

Also derived (not measured — the DBC has no direct longitudinal-accelerometer signal): `EBCMWheelSpdFront.vehicle_speed_mps`/`.longitudinal_accel_mps2` and the same pair for `EBCMWheelSpdRear`, from differentiating each message's own wheel-speed samples. Kept as two independent estimates on purpose (front and rear are separate messages, not synchronized to the same instant) so they can be cross-checked against each other.

## What a session looks like

```
sessions/<session_id>/
    session.json     manifest: start/stop clocks, camera/CAN config, per-stream counters, status
    session.json.lock  empty; makes sharing one manifest between frames and CAN processes safe
    system.csv       once-a-second system state (memory, CPU, disk, temperature), same clock as the ledger
    frames.csv       one row per frame: seq, filename, timestamp_ns
    frames/          00000000_<timestamp_ns>.jpg, 00000001_<timestamp_ns>.jpg, ...
    can.csv          one row per CAN frame, raw only: seq, timestamp_ns, kernel_realtime_ns, can_id, is_extended, is_remote_frame, dlc, data
    decoded.csv      only if you've run can.decode: the same CAN traffic, decoded against a DBC (see below)
```

Frames and CAN are recorded by two independent processes that can share one session this way (`Session.attach()` — see the Quickstart above); either one alone is a normal, complete session missing whichever file it didn't record.

`session.json` has `"status": "recording"` while running and `"complete"` after a clean stop. If it still says `recording` afterwards, the session ended uncleanly. The camera is always recorded as `"calibration": "uncalibrated"`.

**Filenames carry their own timestamp** (added 2026-09-22, `<seq>_<timestamp_ns>.<ext>`), so `frames.csv` is redundant with the files. If it's ever lost, badly torn, or (with a long `--csv-fsync-interval`) simply never flushed before a power cut, rebuild it straight from the frame files:

```
PYTHONPATH=src python3 -m ivision.frames.rebuild sessions/<session_folder>              # report only
PYTHONPATH=src python3 -m ivision.frames.rebuild sessions/<session_folder> --replace     # fix it (backs up the old one to frames.csv.orig)
```

This never invents a timestamp — it only reads what's already in each filename.

To delete demo sessions: `rm -r sessions/`

## Getting a shell on the Pi

The Pi currently rejects this desktop's SSH key. To fix it, run this once on the desktop (it asks for the Pi's password):

```
ssh-copy-id osfield@192.168.0.105
```

Then `ssh osfield@192.168.0.105` should log in without a password. Inside a Claude Code session, type `! ssh-copy-id osfield@192.168.0.105` so it runs in the session.

## Code map

| Path | Purpose |
|---|---|
| `src/ivision/clock.py` | Clock anchors written into each manifest |
| `src/ivision/preflight.py` | Checks memory, swap, temperature, power and disk space before recording and warns |
| `src/ivision/sysmon.py` | Once-a-second memory/CPU/disk/temperature log written to `system.csv` in each session |
| `src/ivision/csvlog.py` | Crash-tolerant CSV writer (shared by every stream) |
| `src/ivision/session.py` | Session directory and manifest; `Session.attach()` lets frames and CAN share one session safely |
| `src/ivision/frames/source.py` | Frame type, the fake camera source, and `IntervalSource` (one frame every N seconds, for calibration shoots) |
| `src/ivision/frames/camera.py` | IMX219 source on Picamera2, proven on real hardware |
| `src/ivision/frames/encode.py` | JPEG encoding: simplejpeg (default) and Pillow (fallback) |
| `src/ivision/frames/camera_probe.py` | Camera check: modes, fps, timestamp clock, encode time |
| `src/ivision/frames/encode_bench.py` | Times JPEG encoders on one real frame, alone and multi-threaded |
| `src/ivision/frames/disk_bench.py` | Writes frame-sized files at 30/s to compare write strategies and find disk stalls |
| `src/ivision/frames/camera_experiments.py` | Runs Picamera2 configurations one per process to find what makes the camera stall |
| `src/ivision/frames/pipeline.py` | Capture queue, writer thread, drop accounting |
| `src/ivision/frames/rebuild.py` | Rebuilds `frames.csv` from frame filenames alone, if the ledger is lost or torn |
| `src/ivision/frames/verify.py` | `verify_session` |
| `src/ivision/frames/record.py` | Command-line recorder; also the shared argument/recording logic `cycle.py` reuses |
| `src/ivision/frames/cycle.py` | Records in cool-down cycles (record N min, cool M min, repeat) |
| `src/ivision/can/source.py` | `CanFrame` type, `SocketCanSource` (real interface), `FakeCanSource`; never transmits |
| `src/ivision/can/pipeline.py` | `CanRecorder`: capture queue, writer thread, drop accounting -- no encode stage, no stall timeout (a quiet bus is expected, not a fault) |
| `src/ivision/can/record.py` | Command-line recorder; `--session <dir>` joins a session `frames.record` already created |
| `src/ivision/can/dump.py` | Live "candump"-style reader for a first-touch check of a bus, no session written |
| `src/ivision/can/decode.py` | Offline DBC decoder (verification only, never part of the live logger); also derives longitudinal acceleration from wheel speed by differentiation |
| `dbc/gm_global_a_powertrain.dbc` | The Malibu's signal definitions (GM Global A platform) |
| `tests/` | Unit tests |
