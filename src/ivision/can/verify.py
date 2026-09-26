"""verify_can_log: check a CAN session's ledger against its manifest.

Mirrors ``frames/verify.py``'s shape and error/warning distinction, adapted
for CAN's different failure modes: there are no per-row files to check (one
CSV *is* the whole log) -- and critically, **a long gap between frames is
not a stall**. The bus goes quiet whenever the car sleeps; that's normal,
not evidence of broken capture. Where ``frames/verify.py`` would warn about
a stall, this instead reports the longest observed gap neutrally, with no
alarm language attached.

    python -m ivision.can.verify <session_dir>

Checks three genuinely different kinds of loss -- easy to conflate, and
this project has already found (2026-09-26, on a real drive) that all
three can happen simultaneously and independently, at real, non-negligible
rates:

  1. **This pipeline's own queue drops** (the writer/disk fell behind the
     capture thread) -- ``can_frames_dropped_queue`` in the manifest,
     reconciled against the ledger row count exactly like
     ``frames/verify.py`` does for frames.
  2. **Kernel/interface-level drops** -- the ``if_stats_before``/``after``
     delta in the manifest (``rx_dropped``, ``rx_errors``,
     ``rx_fifo_errors``). A real CAN controller's small RX buffer
     overflowing under bus load shows up here.
  3. **Untracked socket-buffer loss** -- frames the interface delivered
     (the ``rx_packets`` delta) that never reached this process at all,
     not explained by (1) or (2) either. This is what ``SO_RXQ_OVFL``
     would report if it worked (it doesn't -- see ``can/pipeline.py``'s
     docstring); the only way to see it is this arithmetic gap. Confirmed
     real and nonzero on a real drive (frames+CAN running concurrently
     against the actual vehicle bus), not just a theoretical concern.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from ..session import MANIFEST_NAME
from .pipeline import COLUMNS


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
    """Return parsed rows as (seq, timestamp_ns, can_id), tolerating a torn last row."""
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    lines = text.split("\n")
    torn_tail = None
    if lines and lines[-1] != "":
        torn_tail = lines.pop()  # no trailing newline: the last line is incomplete
    elif lines:
        lines.pop()  # empty string after the final newline
    if not lines:
        report.errors.append("can.csv is empty (no header)")
        return []
    header = next(csv.reader([lines[0]]))
    if tuple(header) != COLUMNS:
        report.errors.append(f"can.csv header is {header}, expected {list(COLUMNS)}")
        return []

    rows = []
    for n, line in enumerate(lines[1:], start=2):
        try:
            cells = next(csv.reader([line]))
            if len(cells) != len(COLUMNS):
                raise ValueError("wrong column count")
            rows.append((int(cells[0]), int(cells[1]), cells[3]))
        except (ValueError, StopIteration):
            is_last = n == len(lines) and torn_tail is None
            if is_last:
                report.warnings.append(f"can.csv line {n} is malformed (torn final row?): {line!r}")
            else:
                report.errors.append(f"can.csv line {n} is malformed: {line!r}")
    if torn_tail:
        report.warnings.append(f"can.csv ends with a torn row (no newline): {torn_tail!r}")
    return rows


def verify_session(session_dir, top_ids: int = 10) -> Report:
    root = Path(session_dir)
    report = Report()

    # --- manifest ---------------------------------------------------------
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

    # --- ledger -------------------------------------------------------------
    ledger = root / "can.csv"
    if not ledger.exists():
        report.errors.append("can.csv not found")
        return report
    rows = _read_ledger(ledger, report)
    report.stats["rows"] = len(rows)

    prev_seq = prev_ts = None
    seq_gaps = 0
    intervals_ns = []
    id_counts: Counter = Counter()
    for seq, ts, can_id in rows:
        id_counts[can_id] += 1
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
    report.stats["distinct_ids"] = len(id_counts)
    if id_counts:
        report.stats["most_frequent_ids"] = dict(id_counts.most_common(top_ids))

    # --- manifest accounting: this pipeline's own queue drops --------------
    stream = ((manifest or {}).get("streams") or {}).get("can") or {}
    captured = None
    reported_drops = False
    if clean:
        if stream.get("can_frames_written") != len(rows):
            report.errors.append(
                f"manifest says {stream.get('can_frames_written')} frames written, "
                f"ledger has {len(rows)}"
            )
        captured = stream.get("can_frames_captured")
        dropped_queue = stream.get("can_frames_dropped_queue")
        unwritten = stream.get("can_frames_unwritten_after_error")
        if None not in (captured, dropped_queue, unwritten):
            if captured != len(rows) + dropped_queue + unwritten:
                report.errors.append(
                    f"manifest accounting mismatch: captured {captured} != ledger rows "
                    f"{len(rows)} + dropped_queue {dropped_queue} + unwritten {unwritten}"
                )
            # The manifest's own counts are authoritative and catch trailing drops
            # (all loss right at the end of a run, before the source stopped) that
            # seq_gaps alone would miss entirely -- a run that drops its last 50
            # frames because the source ended mid-backlog leaves no interior gap
            # for seq_gaps to see, since there's no later row to gap against.
            missing = dropped_queue + unwritten
            reported_drops = True
            if missing:
                report.warnings.append(
                    f"{missing} of {captured} frames ({100 * missing / captured:.1f}%) were "
                    f"dropped, so the capture has gaps. Drops are counted in the manifest, "
                    f"not lost silently"
                )
        report.stats["stop_reason"] = stream.get("stop_reason")

        # --- kernel/interface-level drops, and untracked socket-buffer loss ---
        before, after = stream.get("if_stats_before"), stream.get("if_stats_after")
        if before and after and captured is not None:
            rx_packets_delta = after["rx_packets"] - before["rx_packets"]
            rx_dropped_delta = after["rx_dropped"] - before["rx_dropped"]
            rx_errors_delta = after["rx_errors"] - before["rx_errors"]
            rx_fifo_delta = after["rx_fifo_errors"] - before["rx_fifo_errors"]
            report.stats["interface_rx_packets"] = rx_packets_delta
            if rx_dropped_delta or rx_errors_delta or rx_fifo_delta:
                report.warnings.append(
                    f"kernel/interface-level loss during this session: rx_dropped "
                    f"+{rx_dropped_delta}, rx_errors +{rx_errors_delta}, rx_fifo_errors "
                    f"+{rx_fifo_delta} -- a real controller/driver-level limit, not this "
                    f"pipeline's own queue"
                )
            untracked = rx_packets_delta - rx_dropped_delta - captured
            if untracked > 0:
                report.stats["untracked_socket_loss"] = untracked
                report.warnings.append(
                    f"{untracked} frames arrived at the interface but were never captured "
                    f"by this process, and aren't explained by the kernel's own rx_dropped "
                    f"counter either -- socket-buffer-level loss, distinct from both this "
                    f"pipeline's queue drops and kernel/interface drops (see this module's "
                    f"docstring)"
                )

    # --- drops are accounted for (not errors), but the data has holes -----
    # Fallback only: if the manifest-based check above already ran, it's the
    # authoritative one (it also catches trailing drops, which seq_gaps alone
    # misses). This only fires when that wasn't possible, e.g. a session that
    # didn't end cleanly, or is missing its manifest entirely.
    if not reported_drops and seq_gaps:
        total = len(rows) + seq_gaps
        report.warnings.append(
            f"{seq_gaps} of {total} frames ({100 * seq_gaps / total:.1f}%) were dropped, "
            f"so the capture has gaps. Drops are counted in the manifest, not lost silently"
        )

    # --- timing: informational only -- NOT a stall, a quiet bus is normal ---
    if intervals_ns:
        srt = sorted(intervals_ns)
        report.stats["interval_ms"] = {
            "mean": round(sum(srt) / len(srt) / 1e6, 3),
            "p50": round(_percentile(srt, 50) / 1e6, 3),
            "p99": round(_percentile(srt, 99) / 1e6, 3),
            "max": round(srt[-1] / 1e6, 3),
        }
        report.stats["longest_gap_s"] = round(srt[-1] / 1e9, 2)
        if rows:
            span_s = (rows[-1][1] - rows[0][1]) / 1e9
            if span_s > 0:
                report.stats["achieved_hz"] = round((len(rows) - 1) / span_s, 1)
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Verify a CAN capture session.")
    parser.add_argument("session_dir")
    parser.add_argument("--top-ids", type=int, default=10, help="how many most-frequent IDs to report")
    args = parser.parse_args(argv)
    report = verify_session(args.session_dir, top_ids=args.top_ids)
    print(report.format())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
