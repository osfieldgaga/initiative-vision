"""First-touch camera check. Writes no session; use it before recording.

    PYTHONPATH=src python3 -m ivision.frames.camera_probe --frames 90

It prints:
  * the sensor modes the camera reports (index, size, bit depth, max fps)
  * the achieved frame rate and frame-interval jitter over N frames
  * how far libcamera's SensorTimestamp is from this machine's monotonic and
    boottime clocks (tells you whether the timestamps share our timeline)
  * how long one JPEG encode takes, which bounds fps per encoder thread
Add --save-samples DIR to write three JPEGs to look at with your own eyes.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

from .camera import PicameraSource


def _ms(values_ns) -> dict:
    v = sorted(values_ns)
    if not v:
        return {}
    p99 = v[min(len(v) - 1, round(0.99 * (len(v) - 1)))]
    return {
        "min": round(v[0] / 1e6, 3),
        "median": round(statistics.median(v) / 1e6, 3),
        "mean": round(sum(v) / len(v) / 1e6, 3),
        "p99": round(p99 / 1e6, 3),
        "max": round(v[-1] / 1e6, 3),
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Probe the camera without recording.")
    parser.add_argument("--width", type=int, default=1640)
    parser.add_argument("--height", type=int, default=1232)
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--sensor-mode", type=int, default=None, help="index from the mode list")
    parser.add_argument("--quality", type=int, default=90)
    parser.add_argument("--frames", type=int, default=90, help="frames to time (default 90)")
    parser.add_argument("--encode-samples", type=int, default=10, help="frames to time JPEG encoding on")
    parser.add_argument("--save-samples", metavar="DIR", default=None)
    args = parser.parse_args(argv)

    source = PicameraSource(
        width=args.width, height=args.height, fps=args.fps,
        quality=args.quality, sensor_mode=args.sensor_mode,
    )
    try:
        source.start()
    except ImportError:
        print("picamera2 is not installed here. On the Pi run: sudo apt install -y python3-picamera2")
        return 2
    except Exception as exc:
        print(f"could not start the camera: {exc.__class__.__name__}: {exc}")
        print("Check: ribbon seated and latched, and `rpicam-hello --list-cameras` sees the sensor.")
        return 2

    try:
        print("Sensor modes:")
        for i, m in enumerate(source.sensor_modes()):
            print(f"  [{i}] size={m.get('size')} bit_depth={m.get('bit_depth')} "
                  f"max_fps={m.get('fps')} crop_limits={m.get('crop_limits')}")
        info = source.describe()
        print(f"Requested: {args.width}x{args.height} @ {args.fps} fps, "
              f"sensor_mode={args.sensor_mode}, quality={args.quality}")
        print(f"Applied config: {info.get('applied_config')}")
        print(f"First-frame metadata: {info.get('first_frame_metadata')}")

        stamps, mono_delta, boot_delta, keep = [], [], [], []
        for frame in source.frames():
            mono = time.monotonic_ns()
            boot = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
            stamps.append(frame.timestamp_ns)
            mono_delta.append(mono - frame.timestamp_ns)
            boot_delta.append(boot - frame.timestamp_ns)
            keep.append(frame)
            del keep[:-max(3, args.encode_samples)]
            if len(stamps) >= args.frames:
                break
            if len(stamps) == 1:
                print(f"Frame array shape: {frame.data.shape}, dtype {frame.data.dtype}")
    finally:
        source.stop()
        source.close()

    if len(stamps) < 2:
        print("Not enough frames received to measure anything.")
        return 1

    intervals = [b - a for a, b in zip(stamps, stamps[1:])]
    span_s = (stamps[-1] - stamps[0]) / 1e9
    print(f"\nFrames received: {len(stamps)}")
    print(f"Achieved fps (from sensor timestamps): {(len(stamps) - 1) / span_s:.2f} (target {args.fps})")
    print(f"Frame interval ms: {_ms(intervals)}  (nominal {1000 / args.fps:.3f})")
    print(f"Receive time minus SensorTimestamp, monotonic clock ms: {_ms(mono_delta)}")
    print(f"Receive time minus SensorTimestamp, boottime clock ms:  {_ms(boot_delta)}")
    med_s = abs(statistics.median(mono_delta)) / 1e9
    if med_s < 1.0:
        print("  -> SensorTimestamp is on this machine's monotonic timeline "
              "(delta is capture-to-delivery latency, well under a second).")
    else:
        print("  -> WARNING: SensorTimestamp is NOT close to the monotonic clock. "
              "The timestamp convention needs revisiting before anything else is built on it.")

    sample = keep[-args.encode_samples:]
    times, sizes = [], []
    for frame in sample:
        t0 = time.perf_counter_ns()
        data = source.encode(frame)
        times.append(time.perf_counter_ns() - t0)
        sizes.append(len(data))
    print(f"\nJPEG encode (1 thread, {len(sample)} frames) ms: {_ms(times)}")
    print(f"JPEG size KB: min {min(sizes) / 1024:.0f}, mean {sum(sizes) / len(sizes) / 1024:.0f}, "
          f"max {max(sizes) / 1024:.0f}")
    mean_ms = sum(times) / len(times) / 1e6
    print(f"One encoder thread tops out near {1000 / mean_ms:.1f} fps at this size "
          f"(more threads help only if cores are free).")

    if args.save_samples:
        out = Path(args.save_samples)
        out.mkdir(parents=True, exist_ok=True)
        for label, frame in zip(("a", "b", "c"), (keep[0], keep[len(keep) // 2], keep[-1])):
            (out / f"sample_{label}_seq{frame.seq}.jpg").write_bytes(source.encode(frame))
        print(f"Wrote 3 sample JPEGs to {out}. Open them: check colours (red/blue not swapped), "
              f"sharpness, and that frames are not torn.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
