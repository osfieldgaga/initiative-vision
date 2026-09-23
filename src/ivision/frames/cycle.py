"""Record in cool-down cycles: record for N minutes, pause M minutes to let the SoC
cool, and repeat until the total *recorded* time reaches a target.

Each chunk is its own ordinary session (its own frames.csv, system.csv,
session.json) inside one parent folder, so every existing tool (verify,
rebuild) works unchanged on each chunk. There is no single merged ledger
across chunks; a reader walks the parent folder and treats each subfolder as
one session, same as it already would for ``record``'s output.

Why: sustained JPEG encoding at the Pi 3B's usual sizes heats the SoC to its
85 C throttle cap within a few minutes (2026-09-21/22 testing) and can end in
dropped frames or, at higher loads, the camera stalling outright. Recording in
bounded bursts with a cooldown between them keeps every chunk well inside the
window that tested clean, at the cost of a gap in coverage during each
cooldown — accepted as a trade for reliability.

    python -m ivision.frames.cycle --source picamera2 --width 1280 --height 960 \\
        --quality 75 --encode-workers 2 --record-minutes 5 --cool-minutes 2 \\
        --total-minutes 20

Each chunk's ``Recorder.run()`` opens the camera at the start and closes it at
the end (as every recording already does), so the camera is fully closed
during every cooldown and reopened fresh for the next chunk — a few hundred
milliseconds of exposure settling per chunk, but also a clean camera state
each time rather than one that has been running continuously. If a chunk ends
in an error, the cycle stops there rather than continuing on a camera that
may be in a bad state.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import record


def plan_chunks(record_s: float, cool_s: float, total_s: float) -> list:
    """[(chunk_duration_s, cooldown_after_s), ...]. The last chunk's cooldown is 0.

    The last chunk is shortened so cumulative recorded time never exceeds
    ``total_s``; every other chunk is a full ``record_s``.
    """
    if record_s <= 0 or total_s <= 0:
        return []
    chunks = []
    remaining = total_s
    while remaining > 1e-9:
        length = min(record_s, remaining)
        remaining -= length
        chunks.append([length, cool_s if remaining > 1e-9 else 0.0])
    return chunks


def run_cycle(source, out_dir, args, record_s, cool_s, total_s, status_every=5.0, sleep=time.sleep):
    """Run the whole cycle. Returns a list of (session, summary) for every chunk that ran."""
    chunks = plan_chunks(record_s, cool_s, total_s)
    if not chunks:
        raise ValueError("nothing to record: check --record-minutes and --total-minutes")
    results = []
    try:
        for i, (chunk_s, pause_s) in enumerate(chunks):
            print(f"\n=== chunk {i + 1}/{len(chunks)}: recording {chunk_s / 60:.1f} min ===", flush=True)
            session, summary = record.record_one(source, out_dir, args, duration=chunk_s, status_every=status_every)
            results.append((session, summary))
            if "error" in summary["stop_reason"]:
                print(f"chunk {i + 1} ended in error ({summary['stop_reason']}); stopping the cycle here "
                      f"rather than continuing on a camera that may be in a bad state.", flush=True)
                break
            if pause_s > 0:
                print(f"=== cooling {pause_s / 60:.1f} min (camera idle) ===", flush=True)
                sleep(pause_s)
    finally:
        # Each chunk's Recorder already closes the source when it finishes, so this is
        # normally a no-op (close() is idempotent) — a safety net for an exception raised
        # between chunks, or for a source that was never started (e.g. no chunks to run).
        try:
            source.close()
        except Exception as exc:
            print(f"warning: closing the camera at the end of the cycle raised "
                  f"{exc.__class__.__name__}: {exc}", flush=True)
    return results


def print_cycle_summary(results) -> None:
    total_written = sum(s["frames_written"] for _, s in results)
    total_dropped = sum(s["frames_dropped_queue"] + s["frames_dropped_encode"] for _, s in results)
    print(f"\n=== cycle summary: {len(results)} chunk(s) ===")
    for i, (session, summary) in enumerate(results):
        print(f"  chunk {i + 1}: {session.root.name}  written={summary['frames_written']} "
              f"dropped={summary['frames_dropped_queue'] + summary['frames_dropped_encode']} "
              f"stop_reason={summary['stop_reason']}")
    print(f"  total frames written: {total_written}, dropped: {total_dropped}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Record in cool-down cycles: record N minutes, cool M minutes, "
                    "repeat until a total recorded duration is reached.",
    )
    record.add_camera_args(parser)
    record.add_recorder_args(parser)
    parser.add_argument("--out", default="sessions", help="parent directory; each chunk gets its own session inside it")
    parser.add_argument("--record-minutes", type=float, required=True, help="length of each recording chunk")
    parser.add_argument("--cool-minutes", type=float, required=True, help="idle time between chunks")
    parser.add_argument("--total-minutes", type=float, required=True,
                        help="stop once this much RECORDED time (not wall time) has been reached")
    parser.add_argument("--status-every", type=float, default=5.0,
                        help="seconds between status lines within a chunk (0 = off)")
    args = parser.parse_args(argv)
    record.resolve_encode_workers(args)

    if args.record_minutes <= 0 or args.total_minutes <= 0 or args.cool_minutes < 0:
        parser.error("--record-minutes and --total-minutes must be > 0, --cool-minutes must be >= 0")

    chunks = plan_chunks(args.record_minutes * 60, args.cool_minutes * 60, args.total_minutes * 60)
    wall_s = sum(c + p for c, p in chunks)
    print(f"plan: {len(chunks)} chunk(s), {args.total_minutes:g} min recorded over "
          f"about {wall_s / 60:.1f} min wall time, into {Path(args.out).resolve()}")

    source = record.build_source(args)
    results = run_cycle(
        source, args.out, args,
        record_s=args.record_minutes * 60, cool_s=args.cool_minutes * 60, total_s=args.total_minutes * 60,
        status_every=args.status_every,
    )
    print_cycle_summary(results)
    return 1 if any("error" in s["stop_reason"] for _, s in results) else 0


if __name__ == "__main__":
    sys.exit(main())
