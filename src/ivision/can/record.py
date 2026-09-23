"""Record CAN frames into a new session directory.

    # fake bus, anywhere
    python -m ivision.can.record --source fake --duration 10
    # real interface, on the Pi (bring the interface up first, see below)
    python -m ivision.can.record --source socketcan --interface can0 --duration 60

Before the real interface run, bring the interface up in listen-only mode
(a kernel/controller-level guarantee that this process can never transmit,
in addition to the fact that nothing in this package ever calls send()):

    sudo ip link set can0 type can bitrate 500000 listen-only on
    sudo ip link set can0 up

This process is independent of frame recording -- run both at once (two
commands, two session directories) to capture a synchronized drive. See
``can/pipeline.py``'s module docstring for why that needs no extra
reconciliation step: both streams timestamp with the same system-wide
CLOCK_MONOTONIC.
"""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time

from ..session import Session
from .pipeline import CanRecorder
from .source import FakeCanSource, SocketCanSource


def add_can_args(parser) -> None:
    parser.add_argument("--source", choices=["fake", "socketcan"], default="fake")
    parser.add_argument("--interface", default="can0", help="socketcan only: the interface name")
    parser.add_argument("--fake-hz", type=float, default=100.0, help="fake source only: message rate")
    parser.add_argument("--queue-size", type=int, default=4096,
                        help="CAN frames are tiny and can arrive in bursts far faster than video; "
                             "this can be much larger than the frame queue for the same memory cost")
    parser.add_argument("--csv-fsync-interval", type=float, default=1.0,
                        help="seconds between fsyncs of can.csv; larger values lose more rows in a "
                             "power cut but may avoid write stalls on a slow card")


def build_source(args):
    if args.source == "fake":
        return FakeCanSource(hz=args.fake_hz)
    return SocketCanSource(interface=args.interface)


def make_recorder(session: Session, source, args) -> CanRecorder:
    return CanRecorder(
        session,
        source,
        queue_size=args.queue_size,
        fsync_interval_s=args.csv_fsync_interval,
    )


def status_loop(recorder: CanRecorder, every_s: float, stop: threading.Event) -> None:
    t0 = last_t = time.monotonic()
    last_written = 0
    while not stop.wait(every_s):
        now = time.monotonic()
        written = recorder.written
        hz = (written - last_written) / (now - last_t)
        print(
            f"t={now - t0:6.1f}s  captured={recorder.captured} written={written} hz={hz:6.1f}  "
            f"dropped_queue={recorder.dropped}  backlog={recorder.queue_depth}/{recorder.queue_capacity}",
            flush=True,
        )
        last_t, last_written = now, written


def record_one(source, out_dir, args, duration=None, status_every: float = 5.0):
    session = Session.create(out_dir)
    recorder = make_recorder(session, source, args)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: recorder.request_stop())
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, signal.SIG_IGN)  # see frames/record.py for why
    if duration is not None:
        timer = threading.Timer(duration, recorder.request_stop)
        timer.daemon = True
        timer.start()
    stop_status = threading.Event()
    if status_every > 0:
        threading.Thread(target=status_loop, args=(recorder, status_every, stop_status), daemon=True).start()

    print(f"session: {session.root}", flush=True)
    summary = recorder.run()
    stop_status.set()
    return session, summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Record CAN frames into a session.")
    add_can_args(parser)
    parser.add_argument("--out", default="sessions", help="directory holding sessions")
    parser.add_argument("--duration", type=float, default=None, help="seconds; default: until Ctrl-C")
    parser.add_argument("--status-every", type=float, default=5.0,
                        help="seconds between status lines (0 = off)")
    args = parser.parse_args(argv)

    source = build_source(args)
    session, summary = record_one(source, args.out, args, duration=args.duration, status_every=args.status_every)

    for key, value in summary.items():
        print(f"  {key}: {value}")
    print(f"Check it with: python -m ivision.can.verify {session.root}")
    return 1 if "error" in summary["stop_reason"] else 0


if __name__ == "__main__":
    sys.exit(main())
