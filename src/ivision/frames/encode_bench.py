"""Which JPEG encoder is fast enough on this Pi?

    PYTHONPATH=src python3 -m ivision.frames.encode_bench
    PYTHONPATH=src python3 -m ivision.frames.encode_bench --width 1920 --height 1080

Grabs ONE real frame from the camera (so the content, and therefore the
compressed size and encode time, is realistic), then times each encoder on it,
alone and with several threads running at once. No files are written.

Why this exists: on 2026-09-21 the full recorder shape (capture + 2 Pillow
encoder threads at 1640x1232) delivered only 11 fps, and a longer run stalled
the camera. Encoding is the bottleneck, so pick the encoder with data.

Use --synthetic to run without a camera (numbers then mean little).
"""

from __future__ import annotations

import argparse
import io
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .encode import encode_jpeg_bgr, encode_jpeg_simplejpeg


# -- the encoders under test. Each takes a (h, w, 3) uint8 array and a quality
# -- and returns JPEG bytes. Imports are inside so a missing library just
# -- marks that encoder "not available".

def pillow_bgr(arr, q):
    """What the recorder uses today: Pillow reading raw BGR (a per-pixel unpack)."""
    return encode_jpeg_bgr(arr, q)


def pillow_rgb_noswap(arr, q):
    """Pillow reading the bytes as RGB: no unpack. Colours come out swapped here.

    It measures what Pillow would cost if the camera were configured as BGR888
    (whose memory order is R,G,B), which needs no channel swap.
    """
    import numpy as np
    from PIL import Image

    a = np.ascontiguousarray(arr)
    h, w = a.shape[:2]
    out = io.BytesIO()
    Image.frombuffer("RGB", (w, h), a, "raw", "RGB", 0, 1).save(out, format="JPEG", quality=q)
    return out.getvalue()


def simplejpeg_bgr(arr, q):
    """simplejpeg: a thin wrapper on libjpeg-turbo. The recorder's default encoder."""
    return encode_jpeg_simplejpeg(arr, q)


def opencv_bgr(arr, q):
    import cv2
    import numpy as np

    ok, buf = cv2.imencode(".jpg", np.ascontiguousarray(arr), [cv2.IMWRITE_JPEG_QUALITY, q])
    if not ok:
        raise RuntimeError("cv2.imencode failed")
    return buf.tobytes()


ENCODERS = {
    "pillow_bgr": pillow_bgr,
    "pillow_rgb_noswap": pillow_rgb_noswap,
    "simplejpeg": simplejpeg_bgr,
    "opencv": opencv_bgr,
}


# -- measurement ------------------------------------------------------------

def time_single(fn, arr, quality, seconds):
    fn(arr, quality)  # warm-up: first call pays imports and allocations
    times, size = [], 0
    end = time.perf_counter() + seconds
    while time.perf_counter() < end or len(times) < 3:
        t0 = time.perf_counter()
        data = fn(arr, quality)
        times.append(time.perf_counter() - t0)
        size = len(data)
    assert data[:2] == b"\xff\xd8" and data[-2:] == b"\xff\xd9", "not a JPEG"
    times.sort()
    mean = sum(times) / len(times)
    return {
        "ms_mean": round(mean * 1000, 1),
        "ms_p99": round(times[min(len(times) - 1, int(0.99 * len(times)))] * 1000, 1),
        "fps": round(1 / mean, 1),
        "kb": round(size / 1024),
        "n": len(times),
    }


def time_threads(fn, arr, quality, seconds, threads):
    """Aggregate frames per second with ``threads`` encoders running at once."""
    counts = [0] * threads
    end = time.perf_counter() + seconds

    def work(i):
        while time.perf_counter() < end:
            fn(arr, quality)
            counts[i] += 1

    t0 = time.perf_counter()
    with ThreadPoolExecutor(threads) as pool:
        list(pool.map(work, range(threads)))
    return round(sum(counts) / (time.perf_counter() - t0), 1)


def run_bench(arr, quality=90, seconds=2.0, thread_counts=(2, 4), encoders=None):
    rows = []
    for name, fn in (encoders or ENCODERS).items():
        try:
            single = time_single(fn, arr, quality, seconds)
        except ImportError as exc:
            rows.append({"name": name, "available": False, "why": str(exc)})
            continue
        row = {"name": name, "available": True, **single, "threads": {}}
        for n in thread_counts:
            row["threads"][n] = time_threads(fn, arr, quality, seconds, n)
        rows.append(row)
    return rows


# -- getting a frame --------------------------------------------------------

def synthetic_frame(width, height):
    """A smooth gradient with mild noise. Compresses more easily than a real scene."""
    import numpy as np

    rng = np.random.default_rng(0)
    y, x = np.mgrid[0:height, 0:width]
    base = np.stack([x * 255 // width, y * 255 // height, (x + y) * 255 // (width + height)], axis=2)
    return np.clip(base + rng.integers(-12, 12, base.shape), 0, 255).astype(np.uint8)


def capture_real_frame(width, height, cam_cls=None, frames_to_skip=5):
    if cam_cls is None:
        from picamera2 import Picamera2 as cam_cls
    cam = cam_cls()
    try:
        cam.configure(cam.create_video_configuration(main={"size": (width, height), "format": "RGB888"}))
        cam.start()
        for _ in range(frames_to_skip):  # let exposure settle
            cam.capture_metadata()
        request = cam.capture_request()
        try:
            arr = request.make_array("main").copy()  # copy: do not trust buffer lifetime here
        finally:
            request.release()
        return arr
    finally:
        try:
            cam.stop()
            cam.close()
        except Exception:
            pass


def print_report(arr, quality, rows, seconds):
    h, w = arr.shape[:2]
    print(f"\nFrame {w}x{h}, quality {quality}, {seconds}s per measurement")
    try:
        import os
        print(f"CPU cores: {os.cpu_count()}")
    except Exception:
        pass
    print(f"\n{'encoder':<19}{'ms/frame':>9}{'p99 ms':>8}{'1 thread':>10}{'2 threads':>11}{'4 threads':>11}{'KB':>6}")
    for r in rows:
        if not r["available"]:
            print(f"{r['name']:<19}  not available ({r['why']})")
            continue
        t = r["threads"]
        print(f"{r['name']:<19}{r['ms_mean']:>9}{r['ms_p99']:>8}{r['fps']:>9} f{t.get(2, '-'):>10} f{t.get(4, '-'):>10} f{r['kb']:>5}")
    print("\n(the fps columns are total frames per second across all threads; need > 30 with cores to spare "
          "for the camera and disk. `pillow_rgb_noswap` has swapped colours: it only shows the cost if the "
          "camera is switched to BGR888.)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark JPEG encoders on this machine.")
    parser.add_argument("--width", type=int, default=1640)
    parser.add_argument("--height", type=int, default=1232)
    parser.add_argument("--quality", type=int, default=90)
    parser.add_argument("--seconds", type=float, default=2.0, help="per measurement")
    parser.add_argument("--synthetic", action="store_true", help="no camera; use a generated frame")
    args = parser.parse_args(argv)

    if args.synthetic:
        arr = synthetic_frame(args.width, args.height)
    else:
        try:
            arr = capture_real_frame(args.width, args.height)
        except ImportError:
            print("picamera2 is not installed here. On the Pi run: sudo apt install -y python3-picamera2\n"
                  "(or use --synthetic to benchmark without a camera)")
            return 2
        except Exception as exc:
            print(f"could not capture a frame: {exc.__class__.__name__}: {exc}")
            return 2
    rows = run_bench(arr, args.quality, args.seconds)
    print_report(arr, args.quality, rows, args.seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
