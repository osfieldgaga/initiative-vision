"""First-touch CAN bus check. Logs nothing to disk; use it before recording.

    python -m ivision.can.dump --interface can0
    python -m ivision.can.dump --interface can0 --count 20
    python -m ivision.can.dump --interface can0 --duration 10
    python -m ivision.can.dump --interface can0 --filter 0x1F1:0x7FF
    python -m ivision.can.dump --interface can0 --quiet   # just periodic counts, no per-frame lines

Prints each frame as it arrives (elapsed time, ID, extended/standard, DLC,
data bytes), then a summary on exit: total frames, elapsed time, achieved
rate, and the most frequent arbitration IDs seen -- a first answer to
"is anything actually arriving, and what's on this bus" before trusting a
full recording session to it.

Before running against a real interface, bring it up in listen-only mode
(see can/record.py's module docstring for the exact commands) -- this tool
inherits the same never-transmits guarantee as the rest of the can package,
but listen-only is a stronger, kernel-level version of that promise.
"""

from __future__ import annotations

import argparse
import signal
import sys
import time
from collections import Counter

from .source import CanFrame, SocketCanSource


def _format_id(can_id: int, is_extended: bool) -> str:
    width = 8 if is_extended else 3
    suffix = "x" if is_extended else ""
    return f"{can_id:0{width}X}{suffix}"


def format_frame(frame: CanFrame, elapsed_s: float) -> str:
    data_hex = " ".join(f"{b:02X}" for b in frame.data)
    rtr = " R" if frame.is_remote_frame else ""
    return f"[{elapsed_s:8.3f}] {_format_id(frame.can_id, frame.is_extended):>9}  DLC={frame.dlc}{rtr}  {data_hex}"


def parse_filter(spec: str) -> tuple:
    """'0x1F1:0x7FF' or '1F1:7FF' -> (0x1F1, 0x7FF)."""
    can_id_s, _, mask_s = spec.partition(":")
    if not mask_s:
        raise argparse.ArgumentTypeError(f"filter must be ID:MASK, got {spec!r}")
    return int(can_id_s, 16), int(mask_s, 16)


def run(source, args, clock=time.monotonic, stop_requested=lambda: False) -> int:
    """The dump loop itself, independent of argparse and of what kind of
    source it's reading -- a real SocketCanSource, or a FakeCanSource in
    tests. ``stop_requested`` is polled once per frame (SIGINT sets it in
    ``main()``); a test can pass a simple counter-based lambda instead."""
    try:
        source.start()
    except Exception as exc:
        print(f"could not open {getattr(source, 'interface', '?')}: {exc.__class__.__name__}: {exc}")
        print("Check: interface exists (`ip link show <iface>`), is up, "
              "and this user can access it (try with sudo if it's a permissions error).")
        return 2

    counts: Counter = Counter()  # (can_id, is_extended) -> count
    total = 0
    t0 = clock()
    last_report = t0
    try:
        for frame in source.frames():
            if stop_requested():
                break
            now = clock()
            elapsed = now - t0
            total += 1
            counts[(frame.can_id, frame.is_extended)] += 1
            if not args.quiet:
                print(format_frame(frame, elapsed), flush=True)
            elif now - last_report >= 1.0:
                print(f"[{elapsed:8.1f}] {total} frames so far, {len(counts)} distinct IDs", flush=True)
                last_report = now
            if args.count is not None and total >= args.count:
                break
            if args.duration is not None and elapsed >= args.duration:
                break
    finally:
        source.stop()
        source.close()

    elapsed = clock() - t0
    print(f"\n{total} frames in {elapsed:.1f}s ({total / elapsed if elapsed > 0 else 0:.1f}/s), "
          f"{len(counts)} distinct arbitration IDs")
    if counts:
        print(f"Most frequent (top {args.top}):")
        for (can_id, is_extended), n in counts.most_common(args.top):
            print(f"  {_format_id(can_id, is_extended)}: {n}")
    return 0 if total > 0 else 1


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interface", default="can0")
    parser.add_argument("--count", type=int, default=None, help="stop after this many frames")
    parser.add_argument("--duration", type=float, default=None, help="stop after this many seconds")
    parser.add_argument("--filter", action="append", type=parse_filter, dest="filters", default=None,
                        metavar="ID:MASK", help="kernel-level ID filter, repeatable; default accepts everything")
    parser.add_argument("--quiet", action="store_true", help="don't print every frame, just periodic counts")
    parser.add_argument("--top", type=int, default=10, help="how many most-frequent IDs to show in the summary")
    args = parser.parse_args(argv)

    source = SocketCanSource(interface=args.interface, can_filters=args.filters)
    stop = {"requested": False}
    signal.signal(signal.SIGINT, lambda *_: stop.__setitem__("requested", True))
    return run(source, args, stop_requested=lambda: stop["requested"])


if __name__ == "__main__":
    sys.exit(main())
