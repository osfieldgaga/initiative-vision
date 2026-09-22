# Capture performance: measured results and chosen operating point

All measurements are on the actual hardware: Raspberry Pi 3B, IMX219 on the CSI ribbon, heatsink fitted, internal 32 GB SD card (`SD32G`, dated 04/2025; a USB drive has not yet been tested). Dates below are when each result was measured (2026-09-21/22).

## Chosen operating point

```
--width 1280 --height 960 --quality 75 --encode-workers 2
```
(equivalently: `--source picamera2 --width 1280 --height 960 --quality 75 --encode-workers 2 --csv-fsync-interval 3600`, with the default 256-frame queue)

This is the only configuration tested that ran cleanly with **zero dropped frames of any kind** — no disk drops, no encoder drops — over a full 10-minute continuous recording, and it stayed comfortably under the Pi's thermal limit the whole time rather than approaching it.

| Measured (10 min continuous, 2026-09-22) | Value |
|---|---|
| Frames captured / written | 17,989 / 17,989 (0 dropped) |
| Achieved rate | 30.015 fps |
| Frame interval | mean 33.317 ms, p99 33.338 ms, max 33.627 ms (nominal 33.333 ms) |
| Disk backlog | 0 throughout |
| Peak SoC temperature | 78.4 °C (throttle cap is 85 °C) |
| CPU (user) | 28–34% |
| Mean JPEG size | 67.2 KB/frame |
| Data rate | ~2.0 MB/s (~7.2 GB/hour) |

Also confirmed over a 26-minute cool-down cycle (4× 5 min recording, 3× 2 min cooldown, same settings): all four chunks zero drops, peak 79–81 °C each, and each chunk started from almost the same ~63–65 °C regardless of the previous chunk's heat — the cooldown fully resets the thermal state. See `cycle.py`.

**Not yet confirmed:** the exact sensor crop/FOV that 1280×960 uses. It isn't one of the sensor's native enumerated modes (see below), so Picamera2 derives it from one of them; which one, and what field of view results, hasn't been checked. If framing matters for calibration, run `camera_probe --width 1280 --height 960` and read `applied_config`/`first_frame_metadata` before relying on it.

## Sensor modes (from `camera_probe`, this camera)

| index | size | bit depth | max fps | crop | notes |
|---|---|---|---|---|---|
| 1 | 1640×1232 | 10 | 81.07 | full array, 2×2 binned | full field of view |
| 2 | 1920×1080 | 10 | 47.57 | (680,692)–(1920,1080) | 16:9 crop, narrower FOV |
| 0 | 640×480 | 10 | 200.16 | (1000,752)–(1280,960) | centre crop, narrow FOV |
| 3 | 3280×2464 | 10 | 21.19 | full array, no binning | full resolution |

(8-bit variants of each also exist and were not used.) 640×480 and 1920×1080 both crop the sensor rather than showing its full field of view; only 1640×1232 and 3280×2464 use the whole array.

## What was tried, in order, and why each was rejected

| Configuration | Encoder threads | Result | Why rejected |
|---|---|---|---|
| 1640×1232, q90 | 3 | Frame 348 KB (10.2 MB/s) exceeds the SD card's ~6.4 MB/s sustained write speed | Disk stalls of several seconds once the kernel's write-back buffer fills |
| 1640×1232, q75 | 3 | Disk drops fixed with a 256-frame queue and rare ledger fsync; but thermal throttling set in after ~90–280 s (varied by run) | SoC hit 85 °C, CPU jumped to 80%+ for the same work, encoder drops climbed, and twice the camera stopped delivering frames outright |
| 1920×1080, q75 | 3 | Ran the full 10 minutes with no camera stall, but still throttled (peak 86.5 °C, 719 encoder drops, 4.0%) | 1080p is 2.07 MP vs 1640×1232's 2.02 MP — almost the same pixel count, so almost the same encoding cost; resolution alone didn't fix the heat |
| **1280×960, q75** | **2** | **Zero drops, peak 78.4 °C** | — chosen |

The fix was cutting pixel count *and* encoder threads together. Either change alone (1080p at the same pixel count, or fewer threads at a still-heavy resolution) was tried implicitly along the way and wasn't enough by itself; both together brought CPU load — and with it heat — down by more than either one would have.

## Encoder choice

Measured with `encode_bench` on one real frame from the camera, at 1640×1232:

| Encoder | 1 thread | 2 threads | 4 threads |
|---|---|---|---|
| Pillow | 10.5 fps | 12.6 fps | 14.2 fps |
| **simplejpeg** | **18.0 fps** | **35.7 fps** | **67.2 fps** |

simplejpeg (a thin wrapper on libjpeg-turbo) is the default (`--encoder auto`), with Pillow as an automatic fallback if it isn't installed. Pillow's threads barely help; simplejpeg scales close to linearly with cores. OpenCV was not tested (not installed on this Pi).

## Storage: the SD card's real limit

Measured with `disk_bench`, which paces writes at 30 files/s without a camera involved:
- With `fsync` on every file (no RAM write-back to hide behind): **6.1–6.4 MB/s sustained**, regardless of writer thread count.
- Without fsync (the recorder's normal mode): the kernel's write-back buffer absorbs bursts above that rate for 15–20 seconds, then a multi-second stall follows as it catches up — this is what caused the early 1640×1232/q90 failures.
- The recorder's own per-second `fsync` of the ledger made this worse, not better: each `fsync` forces an ext4 journal commit that must flush pending frame writes first. **`--csv-fsync-interval 3600`** (effectively off; the kernel's own periodic write-back still runs) removed this as a cause entirely. The trade-off is documented in `README.md` and `frames/rebuild.py` exists specifically to recover the ledger from frame filenames (which embed their own timestamp) if a power cut loses recent rows.
- A real SD-card read I/O error was seen once in the kernel log during testing (`mmcblk0 ... I/O error`). Whether that reflects card wear from the volume of test writes, or is unrelated, is unknown. Worth watching.

At the chosen operating point (~2.0 MB/s), the card is nowhere near its limit — a USB drive or SSD is still worth having for longer sessions and to take load off the boot card, but is no longer required to hit 30 fps cleanly.

## Environmental factors that mattered as much as any setting

- **Free memory.** VS Code Server's Remote-SSH connection to the Pi used ~480 MB of the Pi's 905 MB. With it running, camera stalls happened; with it stopped, they didn't recur in three later runs (not conclusively proven, but consistent). `preflight` now warns about this before every recording.
- **Camera DMA memory (CMA).** A separate, fixed 256 MB pool for camera/GPU buffers. A real bug (fixed 2026-09-22): the recorder used to skip closing the camera if a stall left its capture thread stuck, leaking this pool until nothing was left and the Pi needed a reboot. Now always attempted; see `pipeline.py` and its regression test.
- **Heat.** A heatsink is fitted. Even so, 1640×1232 and 1080p at 3 encoder threads still throttled; 1280×960 at 2 threads didn't get close. Active cooling (a fan) has not been tried and would likely raise the ceiling further if higher resolution is ever needed.

## Rotation: measured, and rejected for the Pi

The Pi's ISP can't rotate 90°/270° in hardware (`rpicam-hello --rotation 90` errors with "transforms requiring transpose not supported"). Software rotation was implemented (`encode.py`'s `rotate_cw`, `record --rotate`) and measured on a real 1280x1280 frame before deciding whether to use it:

| Step | Cost |
|---|---|
| JPEG encode alone | 41.4 ms/frame |
| Creating the rotated (transposed) view | 0.08 ms/frame — free |
| Making that view contiguous again (`np.ascontiguousarray`) | **129.6 ms/frame** |
| Rotate + encode together | 171.2 ms/frame |

The rotation itself costs nothing; what's expensive is the memory-layout fix afterward. A 90°/270° rotation is a transpose, which the CPU has to read back in a cache-hostile strided pattern to make contiguous again for the encoder — a property of the Pi 3B's weak memory subsystem, not an inefficiency in the rotation code. On real hardware this took a clean-running 3-thread recorder (0.66% drops) to over 80% dropped frames, and dropping to 2 threads didn't help (ruling out thread contention as the cause — the per-frame cost itself is the problem).

**Decision (2026-09-22): rotation is deferred to Phase 2 (AWS), not done on the Pi.** Frames are saved in the sensor's native orientation; the manifest's `camera.rotate_deg` (0 here) and physical mounting angle are the only record of what correction Phase 2 needs to apply. `--rotate` still exists in the code for completeness and other hardware, but is documented as not recommended for recording on the Pi 3B.

180° rotation (a flip, not a transpose) was not separately measured and its cost is unknown — don't assume it's cheap either without measuring it first.

## Not yet measured

- A USB flash drive or SSD (the disk_bench tool supports `--dir` for this).
- A long (30+ min) single continuous recording at the chosen settings — only a 10-minute continuous run and a 26-minute cycled run have been done.
- Real daylight/moving-vehicle conditions. The only real recordings so far were indoors; auto-exposure in a dim room used the full 33 ms frame time at high analogue gain (noisy, and motion-blur risk if that happened while driving). Worth checking exposure behaviour before relying on footage from a moving car, and fixing it (`--exposure-us`, `--gain`) if needed.
- In-vehicle vibration, sunlight/glare, and the actual power source (Sprint 1 was tested on the Pi's own power situation on a bench; the car test is the first real check of the power bank under this load).
