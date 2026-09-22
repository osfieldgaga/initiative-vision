# Initiative Vision 1: on-vehicle data acquisition

Phase 1 logs camera frames (and later CAN, GPS, IMU) with timestamps on a Raspberry Pi 3B. Plans are in `claude/sprints/`, starting with `00-roadmap.md`.

## Current status (2026-09-22)

Frame capture is proven on real hardware. **Recommended settings**, found after extensive testing (full story in `docs/capture-performance.md`):

```
PYTHONPATH=src python3 -m ivision.frames.record --source picamera2 --width 1280 --height 960 --quality 75 --encode-workers 2
```

This ran a full 10-minute continuous recording with **zero dropped frames** (disk or encoder) and peaked at 78.4°C, well under the Pi's 85°C throttle cap, and a 26-minute cool-down-cycled recording (`cycle.py`) with the same clean result across four chunks. Higher resolutions (1640x1232, 1080p) both hit thermal throttling and dropped frames on this Pi 3B even with a heatsink fitted — see the doc for what was tried and why it was rejected.

Also see `docs/timestamp-convention.md` (needs sign-off) for the timing rules every later stream must follow.

## What works today (Sprint 1, nearly done)

| Status | What |
|---|---|
| Tested on real hardware | Recording (`record.py`), cool-down cycling (`cycle.py`), `verify_session`, `rebuild.py`, encoder/disk/thermal benchmarking tools, `preflight` |
| Tested here (desktop) | The fake-camera pipeline, JPEG encoding, unit tests (160) |
| Doesn't exist yet | CAN, GPS, IMU, upload (later sprints); a real power-cut test on the Pi itself; the code walkthrough doc |

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

**2. Record.** The proven-clean settings (full 10 minutes, zero dropped frames, peak 78°C — see `docs/capture-performance.md`):

```
PYTHONPATH=src python3 -m ivision.frames.record --source picamera2 --width 1280 --height 960 --quality 75 --encode-workers 2 --duration 600
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
| `--width`, `--height` | 1640 x 1232 | Output size. **Use `--width 1280 --height 960` — the only setting proven to run without dropping any frames**; higher resolutions throttle the Pi 3B even with a heatsink (see `docs/capture-performance.md`). `camera_probe` prints the real sensor mode list |
| `--sensor-mode N` | none | Pin the sensor mode by its index in the probe list. **Use this for any run you'll calibrate against**, so the mode can't change between runs |
| `--quality` | 90 | JPEG quality. Use 75 — see the doc for the full size/quality table |
| `--encode-workers` | 3 | JPEG encoder threads. **Use 2** at the recommended resolution; 0 = encode in the capture thread |
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
PYTHONPATH=src python3 -m ivision.frames.cycle --source picamera2 --width 1280 --height 960 --quality 75 --encode-workers 2 --record-minutes 5 --cool-minutes 2 --total-minutes 20
```

Tested for real (2026-09-22): 4× 5-minute chunks, 3× 2-minute cooldowns, 26 minutes wall time — all four chunks zero drops, each starting from nearly the same temperature regardless of how hot the previous one got. It takes every option `record` does (same table as above).

## Starting a recording that survives losing SSH

If you start a recording over SSH and then drive away from Wi-Fi range, the SSH session dies. Two separate things can kill the recording when that happens:

1. **SIGHUP.** Python's default reaction to a lost controlling session is to terminate. Fixed (2026-09-22): the recorder now ignores SIGHUP, so losing the connection alone won't stop it.
2. **A broken output pipe.** If the process is still printing its status line to the now-dead SSH connection, that write can itself fail. Ignoring SIGHUP doesn't fix this — you need to detach the process's output from the SSH session:

```
nohup env PYTHONPATH=src python3 -m ivision.frames.record --source picamera2 --width 1280 --height 960 \
    --quality 75 --encode-workers 2 --duration 1200 > ~/record.log 2>&1 &
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

## What a session looks like

```
sessions/<session_id>/
    session.json     manifest: start/stop clocks, camera config, counters, status
    system.csv       once-a-second system state (memory, CPU, disk, temperature), same clock as the ledger
    frames.csv       one row per frame: seq, filename, timestamp_ns
    frames/          00000000_<timestamp_ns>.jpg, 00000001_<timestamp_ns>.jpg, ...
```

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
| `src/ivision/session.py` | Session directory and manifest |
| `src/ivision/frames/source.py` | Frame type and the fake camera source |
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
| `tests/` | Unit tests |
