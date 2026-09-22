"""Rebuild frames.csv from the frame filenames alone.

Why: since 2026-09-22 each frame filename embeds its own capture timestamp
(``<seq>_<timestamp_ns>.<ext>``), so the ledger is redundant with the files.
If frames.csv is lost, badly torn, or was simply never flushed before a power
cut (more likely now that ``--csv-fsync-interval`` can be large), this
reconstructs it from the frames directory alone. It only reads timestamps
already embedded in filenames — it never invents one.

    python -m ivision.frames.rebuild <session_dir>            # report only, writes nothing
    python -m ivision.frames.rebuild <session_dir> --write     # write frames.rebuilt.csv
    python -m ivision.frames.rebuild <session_dir> --replace   # also replace frames.csv
                                                                 # (backs up any existing one
                                                                 #  to frames.csv.orig first)
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

from .pipeline import COLUMNS, parse_frame_filename


def scan_frames(frames_dir) -> tuple:
    """Return (rows, unparseable_names). rows are (seq, relpath, timestamp_ns), seq-sorted."""
    frames_dir = Path(frames_dir)
    rows, bad = [], []
    if not frames_dir.is_dir():
        return rows, bad
    for p in sorted(frames_dir.iterdir()):
        if p.name.endswith(".tmp"):
            continue  # an in-progress write at the moment of the crash: not a complete frame
        parsed = parse_frame_filename(p.name)
        if parsed is None:
            bad.append(p.name)
            continue
        seq, ts, _ext = parsed
        rows.append((seq, f"frames/{p.name}", ts))
    rows.sort(key=lambda r: r[0])
    return rows, bad


def write_ledger(path, rows) -> None:
    """Write COLUMNS + rows atomically (tmp file, then rename)."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f, lineterminator="\n")
        w.writerow(COLUMNS)
        w.writerows(rows)
    os.replace(tmp, path)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Rebuild frames.csv from frame filenames alone.",
        epilog=__doc__.split("\n\n", 1)[1],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("session_dir")
    parser.add_argument("--write", action="store_true", help="write frames.rebuilt.csv")
    parser.add_argument("--replace", action="store_true",
                        help="also replace frames.csv, backing up any existing one to "
                             "frames.csv.orig first (implies --write)")
    args = parser.parse_args(argv)

    root = Path(args.session_dir)
    rows, bad = scan_frames(root / "frames")
    print(f"frames found: {len(rows)}")
    if bad:
        print(f"filenames that don't match <seq>_<timestamp_ns>.<ext>, skipped: {len(bad)}, "
              f"e.g. {bad[0]}")
    if rows:
        seqs = [r[0] for r in rows]
        missing = seqs[-1] + 1 - len(seqs)
        print(f"seq range: {seqs[0]}..{seqs[-1]} ({missing} missing)")
    else:
        print("nothing to rebuild from")
        return 1

    existing = root / "frames.csv"
    if existing.exists():
        print(f"existing frames.csv has {max(existing.read_text().count(chr(10)) - 1, 0)} rows")

    if not (args.write or args.replace):
        print("\nNo files written. Pass --write to create frames.rebuilt.csv, or --replace to "
              "also replace frames.csv (the existing one is kept as frames.csv.orig).")
        return 0

    rebuilt = root / "frames.rebuilt.csv"
    write_ledger(rebuilt, rows)
    print(f"wrote {rebuilt}")

    if args.replace:
        backup = root / "frames.csv.orig"
        if existing.exists():
            if backup.exists():
                print(f"refusing to overwrite an existing backup at {backup}; frames.csv left as is")
                return 1
            os.replace(existing, backup)
            print(f"backed up the old frames.csv to {backup}")
        os.replace(rebuilt, existing)
        print(f"replaced {existing}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
