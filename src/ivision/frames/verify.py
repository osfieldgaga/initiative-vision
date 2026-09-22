"""verify_session: check a frame session's ledger against its files.

Errors (exit code 1) are things a clean run must never produce. Warnings are
expected after an unclean stop, where only trailing records may be missing.

    python -m ivision.frames.verify <session_dir> [--no-content]
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

from ..session import MANIFEST_NAME
from .pipeline import COLUMNS, parse_frame_filename

JPEG_SOI = b"\xff\xd8"
JPEG_EOI = b"\xff\xd9"


@dataclass
class Report:
    errors: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors

    def format(self) -> str:
        lines = []
        for key, value in self.stats.items():
            lines.append(f"  {key}: {value}")
        for w in self.warnings:
            lines.append(f"WARNING: {w}")
        for e in self.errors:
            lines.append(f"ERROR: {e}")
        lines.append("PASS" if self.ok else "FAIL")
        return "\n".join(lines)


def _percentile(sorted_vals: list, p: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = min(len(sorted_vals) - 1, max(0, round(p / 100 * (len(sorted_vals) - 1))))
    return sorted_vals[idx]


def _read_ledger(path: Path, report: Report):
    """Return parsed rows as (seq, filename, timestamp_ns), tolerating a torn last row."""
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")
    torn_tail = None
    if lines and lines[-1] != "":
        torn_tail = lines.pop()  # no trailing newline: the last line is incomplete
    elif lines:
        lines.pop()  # empty string after the final newline
    if not lines:
        report.errors.append("frames.csv is empty (no header)")
        return []
    header = next(csv.reader([lines[0]]))
    if tuple(header) != COLUMNS:
        report.errors.append(f"frames.csv header is {header}, expected {list(COLUMNS)}")
        return []

    rows = []
    for n, line in enumerate(lines[1:], start=2):
        try:
            cells = next(csv.reader([line]))
            if len(cells) != len(COLUMNS):
                raise ValueError("wrong column count")
            rows.append((int(cells[0]), cells[1], int(cells[2])))
        except (ValueError, StopIteration):
            is_last = n == len(lines) and torn_tail is None
            if is_last:
                report.warnings.append(f"frames.csv line {n} is malformed (torn final row?): {line!r}")
            else:
                report.errors.append(f"frames.csv line {n} is malformed: {line!r}")
    if torn_tail:
        report.warnings.append(f"frames.csv ends with a torn row (no newline): {torn_tail!r}")
    return rows


def _check_jpeg(path: Path) -> str | None:
    size = path.stat().st_size
    if size < 4:
        return f"{path.name}: file is {size} bytes"
    with open(path, "rb") as f:
        head = f.read(2)
        f.seek(-2, 2)
        tail = f.read(2)
    if head != JPEG_SOI or tail != JPEG_EOI:
        return f"{path.name}: missing JPEG start/end marker (truncated?)"
    return None


def verify_session(session_dir, check_content: bool = True) -> Report:
    root = Path(session_dir)
    report = Report()

    # --- manifest -------------------------------------------------------
    manifest = None
    try:
        manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    except FileNotFoundError:
        report.errors.append(f"{MANIFEST_NAME} not found")
    except json.JSONDecodeError as exc:
        report.errors.append(f"{MANIFEST_NAME} is not valid JSON: {exc}")
    clean = bool(manifest) and manifest.get("status") == "complete"
    if manifest and not clean:
        report.warnings.append(
            f"manifest status is {manifest.get('status')!r}: session was not closed cleanly"
        )

    # --- ledger ---------------------------------------------------------
    ledger = root / "frames.csv"
    if not ledger.exists():
        report.errors.append(
            "frames.csv not found (each frame filename embeds its own timestamp, so "
            "`python -m ivision.frames.rebuild` can reconstruct the ledger from them)"
        )
        return report
    rows = _read_ledger(ledger, report)
    report.stats["rows"] = len(rows)

    prev_seq = prev_ts = None
    seq_gaps = 0
    intervals_ns = []
    for seq, name, ts in rows:
        if prev_seq is not None:
            if seq <= prev_seq:
                report.errors.append(f"seq not increasing: {prev_seq} then {seq}")
            else:
                seq_gaps += seq - prev_seq - 1
            if ts <= prev_ts:
                report.errors.append(f"timestamp not increasing at seq {seq}: {prev_ts} then {ts}")
            else:
                intervals_ns.append(ts - prev_ts)
        prev_seq, prev_ts = seq, ts
    report.stats["seq_gaps"] = seq_gaps

    # --- files vs rows --------------------------------------------------
    frames_dir = root / "frames"
    on_disk = {p.name for p in frames_dir.iterdir()} if frames_dir.exists() else set()
    tmp_files = sorted(n for n in on_disk if n.endswith(".tmp"))
    on_disk -= set(tmp_files)
    if tmp_files:
        report.warnings.append(f"{len(tmp_files)} leftover .tmp file(s), e.g. {tmp_files[0]}")

    logged = set()
    for seq, name, ts in rows:
        fname = Path(name).name
        logged.add(fname)
        # The filename carries its own seq and timestamp (2026-09-22), so the ledger row
        # and the file it names must agree; a mismatch means one of them was corrupted
        # or hand-edited, and the two can no longer be trusted to cross-check each other.
        parsed = parse_frame_filename(fname)
        if parsed is None:
            report.errors.append(
                f"seq {seq}: filename {name} doesn't match the <seq>_<timestamp_ns>.<ext> convention"
            )
        elif parsed[:2] != (seq, ts):
            report.errors.append(
                f"seq {seq}: filename says seq {parsed[0]} ts {parsed[1]}, ledger row says seq {seq} ts {ts}"
            )
        path = root / name
        if not path.exists():
            report.errors.append(f"seq {seq}: file {name} is missing")
        elif check_content:
            problem = _check_jpeg(path)
            if problem:
                report.errors.append(f"seq {seq}: {problem}")

    last_seq = rows[-1][0] if rows else -1
    orphans = sorted(on_disk - logged)
    trailing, stray = [], []
    for name in orphans:
        parsed = parse_frame_filename(name)
        (trailing if parsed and parsed[0] > last_seq else stray).append(name)
    if stray:
        report.errors.append(f"{len(stray)} file(s) with no ledger row, e.g. {stray[0]}")
    if trailing:
        msg = f"{len(trailing)} file(s) after the last ledger row, e.g. {trailing[0]}"
        if clean:
            report.errors.append(msg)
        else:
            report.warnings.append(msg + " (expected after an unclean stop)")
    report.stats["files_on_disk"] = len(on_disk)

    # --- manifest accounting -------------------------------------------
    stream = ((manifest or {}).get("streams") or {}).get("frames") or {}
    if clean:
        if stream.get("frames_written") != len(rows):
            report.errors.append(
                f"manifest says {stream.get('frames_written')} frames written, ledger has {len(rows)}"
            )
        # Exact reconciliation: every frame the app received is either a ledger
        # row, counted as dropped (queue full), or counted as unwritten (storage
        # error). This also catches drops at the very start or end of a run,
        # which leave no interior seq gap.
        captured = stream.get("frames_captured")
        dropped_queue = stream.get("frames_dropped_queue")
        dropped_encode = stream.get("frames_dropped_encode") or 0
        unwritten = stream.get("frames_unwritten_after_error")
        last_captured = stream.get("last_seq_captured")
        if None not in (captured, dropped_queue, unwritten):
            dropped = dropped_queue + dropped_encode
            if captured != len(rows) + dropped + unwritten:
                report.errors.append(
                    f"manifest accounting mismatch: captured {captured} != ledger rows "
                    f"{len(rows)} + dropped {dropped} ({dropped_queue} queue, "
                    f"{dropped_encode} encode) + unwritten {unwritten}"
                )
        if captured is not None and last_captured is not None and captured != last_captured + 1:
            report.errors.append(
                f"source seq is not contiguous: {captured} frames captured, last seq {last_captured}"
            )
        if last_captured is not None:
            report.stats["missing_seq_total"] = last_captured + 1 - len(rows)
        report.stats["stop_reason"] = stream.get("stop_reason")

    # --- drops are accounted for (not errors) but the data has holes -------
    missing = report.stats.get("missing_seq_total", seq_gaps)
    if missing:
        total = len(rows) + missing
        report.warnings.append(
            f"{missing} of {total} frames ({100 * missing / total:.1f}%) were dropped, "
            f"so the capture has gaps. Drops are counted in the manifest, not lost silently"
        )

    # --- timing ---------------------------------------------------------
    if intervals_ns:
        nominal_fps = ((manifest or {}).get("camera") or {}).get("target_fps")
        srt = sorted(intervals_ns)
        nominal_ns = 1e9 / nominal_fps if nominal_fps else _percentile(srt, 50)
        report.stats["nominal_interval_ms"] = round(nominal_ns / 1e6, 3)
        report.stats["interval_ms"] = {
            "mean": round(sum(srt) / len(srt) / 1e6, 3),
            "p50": round(_percentile(srt, 50) / 1e6, 3),
            "p99": round(_percentile(srt, 99) / 1e6, 3),
            "max": round(srt[-1] / 1e6, 3),
        }
        bins = {"<0.5x": 0, "0.5-0.9x": 0, "0.9-1.1x": 0, "1.1-1.5x": 0, "1.5-2.5x": 0, ">=2.5x": 0}
        suspected_missing = 0
        for dt in srt:
            r = dt / nominal_ns
            if r < 0.5:
                bins["<0.5x"] += 1
            elif r < 0.9:
                bins["0.5-0.9x"] += 1
            elif r < 1.1:
                bins["0.9-1.1x"] += 1
            elif r < 1.5:
                bins["1.1-1.5x"] += 1
            elif r < 2.5:
                bins["1.5-2.5x"] += 1
            else:
                bins[">=2.5x"] += 1
            if r >= 1.5:
                suspected_missing += round(r) - 1
        report.stats["interval_histogram"] = bins
        if srt[-1] > 1e9:
            report.warnings.append(
                f"longest gap between frames is {srt[-1] / 1e9:.2f} s "
                f"(nominal {nominal_ns / 1e6:.1f} ms): a stall in the recording"
            )
        # Frames lost upstream of the app (e.g. inside libcamera) leave no seq
        # gap but do leave a timing gap; this estimates them.
        report.stats["suspected_missing_from_timing"] = suspected_missing
        if rows:
            span_s = (rows[-1][2] - rows[0][2]) / 1e9
            if span_s > 0:
                report.stats["achieved_fps"] = round((len(rows) - 1) / span_s, 3)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verify a frame capture session.")
    parser.add_argument("session_dir")
    parser.add_argument("--no-content", action="store_true", help="skip JPEG marker check")
    args = parser.parse_args(argv)
    report = verify_session(args.session_dir, check_content=not args.no_content)
    print(report.format())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
