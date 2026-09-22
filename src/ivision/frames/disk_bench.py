"""Can this disk absorb 30 frame files a second? Reproduces the recorder's disk
load without the camera, so write strategies can be compared.

    PYTHONPATH=src python3 -m ivision.frames.disk_bench
    PYTHONPATH=src python3 -m ivision.frames.disk_bench --only A_current --seconds 60

Why: on 2026-09-21 a 1640x1232 recording ran at 30 fps for 18 s, then the writer
froze for about 9 s and 250 frames were dropped. Files had been landing at
10.7 MB/s; then almost nothing. The likely cause is the kernel's write-back
throttle: it lets about 180 MB of dirty pages build up in RAM, then blocks
writers until the SD card catches up.

Each variant writes files of frame size at a fixed rate (default 30/s, 348 KB,
the real frame size at 1640x1232 q90) through a bounded queue exactly like the
recorder, and reports per-5-second throughput, the slowest single write, and
how many frames were dropped because the queue filled. About 40 s and 400 MB
per variant; the test files are deleted afterwards.
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# name -> how each file is written
VARIANTS = {
    "A_current": dict(rename=True, fsync=False, writers=1),      # what the recorder does now
    "B_no_rename": dict(rename=False, fsync=False, writers=1),   # skip the tmp+rename step
    "C_fsync": dict(rename=True, fsync=True, writers=1),         # force each file to disk
    "D_fsync_x3": dict(rename=True, fsync=True, writers=3),      # ... with 3 writer threads
}
DESCRIPTIONS = {
    "A_current": "tmp file, write, rename; 1 writer (current recorder)",
    "B_no_rename": "write straight to the final name; 1 writer",
    "C_fsync": "tmp+rename and fsync every file; 1 writer",
    "D_fsync_x3": "tmp+rename and fsync every file; 3 writers",
}
_STOP = object()


def write_one(path: Path, data: bytes, rename: bool, fsync: bool) -> None:
    target = path.with_name(path.name + ".tmp") if rename else path
    with open(target, "wb") as f:
        f.write(data)
        if fsync:
            f.flush()
            os.fsync(f.fileno())
    if rename:
        os.replace(target, path)


def windows(records, window_s=5.0):
    """records: list of (t_done_s, latency_s, nbytes). Returns per-window stats."""
    if not records:
        return []
    n = int(max(r[0] for r in records) // window_s) + 1
    out = [{"start": i * window_s, "files": 0, "bytes": 0, "max_ms": 0.0} for i in range(n)]
    for t, lat, size in records:
        w = out[int(t // window_s)]
        w["files"] += 1
        w["bytes"] += size
        w["max_ms"] = max(w["max_ms"], lat * 1000)
    for w in out:
        w["mb_s"] = round(w["bytes"] / window_s / 1e6, 2)
    return out


def run_variant(directory, cfg, seconds=40.0, rate=30.0, size_bytes=348 * 1024,
                queue_size=64, write=write_one):
    """Write paced files with ``cfg``; return a result dict. Cleans up after itself."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    payload = os.urandom(size_bytes)
    q: queue.Queue = queue.Queue(maxsize=queue_size)
    records = []  # (t_done, latency, nbytes); list.append is thread-safe
    start = time.perf_counter()

    def writer():
        while True:
            item = q.get()
            if item is _STOP:
                return
            t0 = time.perf_counter()
            write(root / f"{item:08d}.bin", payload, cfg["rename"], cfg["fsync"])
            t1 = time.perf_counter()
            records.append((t1 - start, t1 - t0, size_bytes))

    threads = [threading.Thread(target=writer, daemon=True) for _ in range(cfg["writers"])]
    for t in threads:
        t.start()

    produced = dropped = 0
    interval = 1.0 / rate
    next_t = start
    while time.perf_counter() - start < seconds:
        try:
            q.put_nowait(produced)
        except queue.Full:
            dropped += 1
        produced += 1
        next_t += interval
        delay = next_t - time.perf_counter()
        if delay > 0:
            time.sleep(delay)
    produce_end = time.perf_counter() - start
    for _ in threads:
        q.put(_STOP)
    for t in threads:
        t.join()
    drain_s = time.perf_counter() - start - produce_end
    shutil.rmtree(root, ignore_errors=True)

    lats = sorted(r[1] for r in records)
    pick = lambda p: lats[min(len(lats) - 1, int(p * len(lats)))] * 1000 if lats else 0.0
    return {
        "produced": produced,
        "written": len(records),
        "dropped": dropped,
        "drop_pct": round(100 * dropped / produced, 1) if produced else 0.0,
        "mb_s_overall": round(len(records) * size_bytes / max(produce_end + drain_s, 1e-9) / 1e6, 2),
        "lat_ms_p50": round(pick(0.5), 1),
        "lat_ms_p99": round(pick(0.99), 1),
        "lat_ms_max": round(lats[-1] * 1000, 1) if lats else 0.0,
        "stalls_over_500ms": sum(1 for x in lats if x > 0.5),
        "drain_s": round(drain_s, 1),
        "windows": windows(records),
    }


def find_mount(path: str, mounts_text: str):
    """The (device, mountpoint, fstype, options) whose mountpoint is the longest
    prefix of ``path``, from the text of /proc/mounts. None if nothing matches."""
    best = None
    for line in mounts_text.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        dev, mnt, fstype, opts = parts[0], parts[1].replace("\\040", " "), parts[2], parts[3]
        inside = path == mnt or path.startswith(mnt.rstrip("/") + "/")
        if inside and (best is None or len(mnt) > len(best[1])):
            best = (dev, mnt, fstype, opts)
    return best


def base_block_device(dev: str):
    """/dev/sda1 -> sda, /dev/mmcblk0p2 -> mmcblk0. None for anything else."""
    name = dev.rsplit("/", 1)[-1]
    if name.startswith("mmcblk") or name.startswith("nvme"):
        return re.sub(r"p\d+$", "", name)
    if re.match(r"^sd[a-z]+\d*$", name):
        return re.sub(r"\d+$", "", name)
    return None


def system_info(directory) -> list:
    """Facts about the disk being tested and the kernel write-back settings. Never raises."""
    lines = []

    def read(path):
        try:
            return Path(path).read_text().strip()
        except OSError:
            return None

    try:
        usage = shutil.disk_usage(directory)
        lines.append(f"free space: {usage.free / 1e9:.1f} GB of {usage.total / 1e9:.1f} GB")
    except OSError:
        pass
    mounts = read("/proc/mounts")
    mount = find_mount(str(Path(directory).resolve()), mounts) if mounts else None
    if mount:
        dev, mnt, fstype, opts = mount
        lines.append(f"writing to: {dev} mounted at {mnt}, filesystem {fstype}")
        lines.append(f"mount options: {opts}")
        block = base_block_device(dev)
        if block:
            base = f"/sys/block/{block}/device"
            details = {k: read(f"{base}/{k}") for k in ("vendor", "model", "name", "manfid", "date")}
            shown = " ".join(f"{k}={v}" for k, v in details.items() if v)
            if shown:
                lines.append(f"device: {shown}")
    for key in ("dirty_ratio", "dirty_background_ratio", "dirty_bytes", "dirty_background_bytes"):
        v = read(f"/proc/sys/vm/{key}")
        if v is not None:
            lines.append(f"vm.{key} = {v}")
    mem = read("/proc/meminfo")
    if mem:
        first = mem.splitlines()[0].split()
        lines.append(f"memory: {int(first[1]) // 1024} MB total")
    for cmd in (["vcgencmd", "get_throttled"], ["vcgencmd", "measure_temp"]):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=3).stdout.strip()
            if out:
                lines.append(out)
        except (OSError, subprocess.SubprocessError):
            pass
    return lines


def print_result(name, result, rate, seconds):
    print(f"\n[{name}] {DESCRIPTIONS.get(name, '')}")
    print(f"  produced {result['produced']}, written {result['written']}, "
          f"DROPPED {result['dropped']} ({result['drop_pct']}%), overall {result['mb_s_overall']} MB/s, "
          f"backlog drain {result['drain_s']} s")
    print(f"  per-file write latency ms: p50 {result['lat_ms_p50']}, p99 {result['lat_ms_p99']}, "
          f"max {result['lat_ms_max']}; writes over 500 ms: {result['stalls_over_500ms']}")
    print("  MB/s per 5 s window: " + " ".join(f"{w['mb_s']:g}" for w in result["windows"]))
    print("  worst write in each window (ms): " + " ".join(f"{w['max_ms']:.1f}" for w in result["windows"]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Benchmark frame-file writing on this disk.")
    parser.add_argument("--dir", default="_disk_bench", help="test folder (created and deleted). "
                        "Put it on the disk you would record to, e.g. --dir /media/osfield/USBDRIVE/_disk_bench")
    parser.add_argument("--seconds", type=float, default=40.0, help="per variant")
    parser.add_argument("--rate", type=float, default=30.0, help="files per second")
    parser.add_argument("--size-kb", type=int, default=348, help="file size (348 = 1640x1232 q90)")
    parser.add_argument("--only", nargs="+", metavar="VARIANT")
    parser.add_argument("--list", action="store_true")
    args = parser.parse_args(argv)

    if args.list:
        for name, desc in DESCRIPTIONS.items():
            print(f"{name}: {desc}")
        return 0
    names = args.only or list(VARIANTS)
    unknown = [n for n in names if n not in VARIANTS]
    if unknown:
        print(f"unknown variant(s) {unknown}; use --list")
        return 2

    print("System:")
    for line in system_info(Path(args.dir).resolve().parent):
        print("  " + line)
    total_gb = len(names) * args.seconds * args.rate * args.size_kb / 1e6
    print(f"\nWill write about {total_gb:.1f} GB in total ({args.rate:g} files/s x {args.size_kb} KB "
          f"for {args.seconds:g} s per variant); the test files are deleted after each variant.")
    results = {}
    for name in names:
        print(f"\nrunning {name} ...", flush=True)
        results[name] = run_variant(args.dir, VARIANTS[name], args.seconds, args.rate, args.size_kb * 1024)
        print_result(name, results[name], args.rate, args.seconds)
        time.sleep(3)  # let the card settle between variants

    print("\n=== Summary (need: 0 dropped) ===")
    print(f"{'variant':<13}{'dropped':>8}{'drop %':>8}{'MB/s':>7}{'worst write ms':>16}{'>500ms':>8}")
    for name, r in results.items():
        print(f"{name:<13}{r['dropped']:>8}{r['drop_pct']:>8}{r['mb_s_overall']:>7}"
              f"{r['lat_ms_max']:>16}{r['stalls_over_500ms']:>8}")
    for line in system_info(Path(args.dir).resolve().parent)[-2:]:
        print("  after: " + line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
