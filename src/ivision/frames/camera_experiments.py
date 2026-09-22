"""Find out why the camera stalls under our code when rpicam-hello does not.

    PYTHONPATH=src python3 -m ivision.frames.camera_experiments

Runs a set of Picamera2 configurations, each in its own process (a stalled
camera can hang, and libcamera state should not leak between runs), each
differing from ``A`` in one way. For each it reports frames delivered in the
window, time to first frame, and libcamera "timed out" errors.

Options: --seconds N (window per config, default 6), --only A D I (subset),
--list (show configs). This talks to Picamera2 directly, not through the
recorder, so a config that works here but not in ``record`` points at the
recorder, and vice versa.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time

_BASE = dict(
    size=(1640, 1232),
    fmt="RGB888",
    fdl=True,        # fixed FrameDurationLimits (33333 us)
    queue=None,      # None = Picamera2 default
    buffers=6,
    array=True,      # call make_array on every frame
    enum=True,       # read cam.sensor_modes before configuring
    meta=True,       # capture_metadata() once after start
    thread=False,    # capture loop in a worker thread instead of the main thread
    encode=0,        # JPEG encoder threads (0 = none)
)

CONFIGS = {
    "A": (dict(), "closest to `record`: RGB888, fixed 30 fps, capture in main thread, no encode"),
    "B": (dict(array=False), "as A, but never touch pixel data (metadata only)"),
    "C": (dict(fmt="YUV420", array=False), "YUV420 stream, the format rpicam-hello uses"),
    "D": (dict(fdl=False), "as A, no fixed FrameDurationLimits"),
    "E": (dict(queue=False), "as A, queue=False"),
    "F": (dict(buffers=12), "as A, 12 buffers"),
    "G": (dict(enum=False), "as A, skip the sensor_modes enumeration"),
    "H": (dict(meta=False), "as A, skip the first capture_metadata()"),
    "I": (dict(thread=True), "as A, capture loop in a worker thread (as the recorder does)"),
    "J": (dict(thread=True, encode=2), "as I plus 2 JPEG encoder threads (the full recorder shape)"),
    "K": (dict(size=(640, 480)), "as A at 640x480"),
}


def _cfg(name: str) -> dict:
    return {**_BASE, **CONFIGS[name][0]}


# --------------------------------------------------------------------------
# Child: one configuration, in this process.
# --------------------------------------------------------------------------

def run_child(name, seconds, cam_cls=None, out=print, exit_process=True) -> dict:
    cfg = _cfg(name)
    if cam_cls is None:
        from picamera2 import Picamera2 as cam_cls

    state = {"n": 0, "first": None, "stop": False, "error": None, "encoded": 0, "dropped_encode": 0}
    t_begin = time.monotonic()
    cam = cam_cls()
    if cfg["enum"]:
        cam.sensor_modes  # noqa: B018  (property with side effects on the real camera)
    kw = {"main": {"size": cfg["size"], "format": cfg["fmt"]}, "buffer_count": cfg["buffers"]}
    if cfg["fdl"]:
        kw["controls"] = {"FrameDurationLimits": (33333, 33333)}
    if cfg["queue"] is not None:
        kw["queue"] = cfg["queue"]
    cam.configure(cam.create_video_configuration(**kw))
    t_start = time.monotonic()
    cam.start()
    start_s = time.monotonic() - t_start
    if cfg["meta"]:
        cam.capture_metadata()
    t0 = time.monotonic()  # the measured window starts here

    pool = sem = None
    if cfg["encode"]:
        from concurrent.futures import ThreadPoolExecutor
        from .encode import encode_jpeg_bgr

        pool = ThreadPoolExecutor(cfg["encode"])
        sem = threading.BoundedSemaphore(cfg["encode"] * 2)

        def job(arr):
            try:
                encode_jpeg_bgr(arr, 90)
                state["encoded"] += 1
            finally:
                sem.release()

    def loop(deadline=None):
        try:
            while not state["stop"] and (deadline is None or time.monotonic() < deadline):
                req = cam.capture_request()
                try:
                    req.get_metadata()
                    arr = req.make_array("main") if cfg["array"] else None
                finally:
                    req.release()
                if state["first"] is None:
                    state["first"] = time.monotonic() - t0
                state["n"] += 1
                if pool is not None and arr is not None:
                    if sem.acquire(blocking=False):
                        pool.submit(job, arr)
                    else:
                        state["dropped_encode"] += 1
        except Exception as exc:  # report it; do not die silently
            state["error"] = f"{exc.__class__.__name__}: {exc}"

    def ticker():
        while not state["stop"]:
            time.sleep(1.0)
            out(f"TICK t={time.monotonic() - t0:.0f} frames={state['n']}")

    threading.Thread(target=ticker, daemon=True).start()
    deadline = t0 + seconds
    if cfg["thread"]:
        worker = threading.Thread(target=loop, daemon=True)
        worker.start()
        while time.monotonic() < deadline and worker.is_alive():
            time.sleep(0.05)
    else:
        # In the main thread. If the camera stalls this blocks, and the parent
        # reports HUNG with the last tick it saw.
        loop(deadline)
    state["stop"] = True

    elapsed = time.monotonic() - t0
    result = {
        "name": name,
        "frames": state["n"],
        "seconds": round(elapsed, 2),
        "fps": round(state["n"] / elapsed, 1) if elapsed else 0.0,
        "first_frame_s": None if state["first"] is None else round(state["first"], 2),
        "start_s": round(start_s, 2),
        "setup_s": round(t_start - t_begin, 2),
        "error": state["error"],
        "encoded": state["encoded"],
        "dropped_encode": state["dropped_encode"],
    }
    out("RESULT " + json.dumps(result))
    if exit_process:
        # Skip a clean shutdown: a stalled camera can hang cam.stop(), and the
        # result is already printed.
        sys.stdout.flush()
        os._exit(0)
    try:
        cam.stop()
        cam.close()
    except Exception:
        pass
    return result


# --------------------------------------------------------------------------
# Parent: run each config in its own process and summarise.
# --------------------------------------------------------------------------

def parse_output(text: str) -> dict:
    """Pull the result (or, if the child hung, the last tick) out of its output."""
    result = None
    last_tick = None
    for line in text.splitlines():
        if line.startswith("RESULT "):
            result = json.loads(line[len("RESULT "):])
        elif line.startswith("TICK "):
            last_tick = line
    return {
        "result": result,
        "last_tick": last_tick,
        "timeouts": text.count("timed out"),
    }


def verdict(parsed: dict, hung: bool, seconds: float) -> str:
    result = parsed["result"]
    if result is None:
        return "HUNG" if hung else "CRASHED"
    if result["error"]:
        return "ERROR"
    fps = result["fps"]
    if fps >= 27 and not parsed["timeouts"]:
        return "OK"
    if result["frames"] <= 10:
        return "STALL"
    return "SLOW"


def run_all(names, seconds, python=sys.executable) -> list:
    rows = []
    for name in names:
        desc = CONFIGS[name][1]
        print(f"[{name}] {desc} ...", flush=True)
        env = dict(os.environ, LIBCAMERA_LOG_LEVELS="*:ERROR")
        cmd = [python, "-m", "ivision.frames.camera_experiments", "--one", name, "--seconds", str(seconds)]
        hung, text = False, ""
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 30, env=env)
            text = proc.stdout + proc.stderr
        except subprocess.TimeoutExpired as exc:
            hung = True
            def _s(v):
                return v.decode(errors="replace") if isinstance(v, bytes) else (v or "")
            text = _s(exc.stdout) + _s(exc.stderr)
        parsed = parse_output(text)
        rows.append((name, desc, parsed, verdict(parsed, hung, seconds), text))
        r = parsed["result"]
        print(f"     -> {rows[-1][3]}"
              + (f"  {r['frames']} frames, {r['fps']} fps, first frame {r['first_frame_s']}s" if r else
                 f"  last progress: {parsed['last_tick']}")
              + (f", {parsed['timeouts']} libcamera timeouts" if parsed["timeouts"] else ""), flush=True)
        time.sleep(2)  # let the camera settle between processes
    return rows


def print_summary(rows, seconds) -> None:
    print("\n=== Summary ===")
    print(f"{'cfg':<4}{'verdict':<9}{'frames':>7}{'fps':>7}{'first(s)':>10}{'start(s)':>10}{'timeouts':>10}  description")
    for name, desc, parsed, v, _ in rows:
        r = parsed["result"] or {}
        print(f"{name:<4}{v:<9}{r.get('frames', '-'):>7}{r.get('fps', '-'):>7}"
              f"{str(r.get('first_frame_s', '-')):>10}{str(r.get('start_s', '-')):>10}"
              f"{parsed['timeouts']:>10}  {desc}")
        if r.get("error"):
            print(f"      error: {r['error']}")
        if r.get("encoded"):
            print(f"      encoded={r['encoded']} dropped_encode={r['dropped_encode']}")
    print(f"\n(window {seconds}s per config; OK means >= 27 fps and no libcamera timeouts)")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Find why the camera stalls under our code.")
    parser.add_argument("--seconds", type=float, default=6.0)
    parser.add_argument("--only", nargs="+", metavar="CFG")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--one", metavar="CFG", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.list:
        for name, (_, desc) in CONFIGS.items():
            print(f"{name}: {desc}")
        return 0
    if args.one:
        run_child(args.one, args.seconds)
        return 0
    names = args.only or list(CONFIGS)
    unknown = [n for n in names if n not in CONFIGS]
    if unknown:
        print(f"unknown config(s): {unknown}; use --list")
        return 2
    try:
        import picamera2  # noqa: F401
    except ImportError:
        print("picamera2 is not installed here. On the Pi run: sudo apt install -y python3-picamera2")
        return 2
    rows = run_all(names, args.seconds)
    print_summary(rows, args.seconds)
    return 0


if __name__ == "__main__":
    sys.exit(main())
