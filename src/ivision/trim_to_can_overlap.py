"""Delete frames recorded outside the time window CAN was also recording.

Frames and CAN are independent processes that can share one session
(``Session.attach()``) but don't have to start or stop at exactly the same
instant -- one might join a few seconds after the other, or stop a few
seconds before it. That leaves a leading and/or trailing stretch of frames
with no CAN coverage at all: useless for anything that needs both streams
together. This deletes those frames, so every remaining one falls within
the window where both streams were genuinely running.

    python -m ivision.trim_to_can_overlap sessions/<id>              # report only, deletes nothing
    python -m ivision.trim_to_can_overlap sessions/<id> --delete     # actually delete

This is a destructive, irreversible operation on real recorded data --
defaults to a dry run, same pattern as ``frames.rebuild``. When run with
``--delete``, it removes the frame files, rewrites ``frames.csv`` to match
what's left, and updates ``session.json``'s ``frames`` stream (via
``Session.attach()``/``flush()``, the same safe path everything else in
this project uses to touch the manifest) so a later ``frames.verify`` run
doesn't spuriously flag a mismatch -- the manifest explicitly records that
this was a deliberate trim, with counts, not silently adjusted numbers with
no explanation. ``frames_captured`` and the drop counters are left
untouched: they describe what the original recording actually did, which
trimming doesn't change; only ``frames_written`` (what's actually on disk
now) is updated.

"Matching" is a broad time-range overlap -- does this frame's timestamp
fall between the earliest and latest CAN row's timestamp -- not a check
that a specific nearby CAN row exists for each individual frame. CAN
traffic is dense enough in practice (see ``can/verify.py``'s
``achieved_hz``) that within its own recording window, coverage is
effectively continuous except during genuinely expected quiet-bus
stretches (ignition off, car asleep) -- this deliberately does not try to
detect those frame-by-frame, only the leading/trailing gap from streams
starting/stopping independently.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

from .session import Session


def _timestamp_range(csv_path: Path, ts_column: int) -> tuple:
    """(first, last) timestamp_ns among a CSV's data rows."""
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        next(reader)  # header
        first = last = None
        for row in reader:
            ts = int(row[ts_column])
            if first is None:
                first = ts
            last = ts
    if first is None:
        raise ValueError(f"{csv_path} has no data rows")
    return first, last


def compute_overlap(session_dir: Path) -> tuple:
    """(start_ns, end_ns): the overlap between frames.csv's and can.csv's
    own timestamp ranges."""
    frame_start, frame_end = _timestamp_range(session_dir / "frames.csv", ts_column=2)
    can_start, can_end = _timestamp_range(session_dir / "can.csv", ts_column=1)
    return max(frame_start, can_start), min(frame_end, can_end)


def trim_session(session_dir, delete: bool = False) -> dict:
    """Reports (and, with delete=True, removes) frames outside the CAN
    overlap window. Returns a summary dict (also what the CLI prints)."""
    session_dir = Path(session_dir)
    frames_csv = session_dir / "frames.csv"
    can_csv = session_dir / "can.csv"
    if not frames_csv.exists():
        raise FileNotFoundError(f"{frames_csv} not found -- nothing to trim")
    if not can_csv.exists():
        raise FileNotFoundError(f"{can_csv} not found -- nothing to trim against")

    start_ns, end_ns = compute_overlap(session_dir)

    with open(frames_csv, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        header = next(reader)
        rows = list(reader)

    kept, dropped = [], []
    for row in rows:
        (kept if start_ns <= int(row[2]) <= end_ns else dropped).append(row)

    summary = {
        "total_frames": len(rows),
        "kept": len(kept),
        "dropped": len(dropped),
        "overlap_start_ns": start_ns,
        "overlap_end_ns": end_ns,
        "deleted": False,
    }
    if not delete or not dropped:
        return summary

    frames_dir = session_dir / "frames"
    for row in dropped:
        try:
            (frames_dir / Path(row[1]).name).unlink()
        except FileNotFoundError:
            pass  # already gone; still drop its ledger row below

    with open(frames_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(kept)

    try:
        session = Session.attach(session_dir)
    except FileNotFoundError:
        pass  # no session.json (e.g. a hand-built test session) -- file changes above still stand
    else:
        session.set_stream(
            "frames",
            frames_written=len(kept),
            trimmed_to_can_overlap={
                "dropped": len(dropped), "kept": len(kept),
                "overlap_start_ns": start_ns, "overlap_end_ns": end_ns,
            },
        )
        session.flush()

    summary["deleted"] = True
    return summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("session_dir")
    parser.add_argument("--delete", action="store_true", help="actually delete (default: report only)")
    args = parser.parse_args(argv)

    summary = trim_session(args.session_dir, delete=args.delete)

    print(f"frames: {summary['total_frames']} total, {summary['kept']} within the CAN overlap window, "
          f"{summary['dropped']} outside it")
    print(f"overlap window: {summary['overlap_start_ns']} - {summary['overlap_end_ns']} (timestamp_ns)")
    if summary["dropped"] == 0:
        print("nothing to trim")
    elif summary["deleted"]:
        print(f"deleted {summary['dropped']} frame file(s), rewrote frames.csv, updated session.json")
    else:
        print(f"dry run: {summary['dropped']} frame file(s) would be deleted. Re-run with --delete to do it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
