"""Record frames into a new session directory.

    # fake camera, anywhere
    python -m ivision.frames.record --source fake --duration 10
    # real camera, on the Pi
    python -m ivision.frames.record --source picamera2 --duration 60

Run ``camera_probe`` first to check the camera works and to see the sensor modes.
For recording in cool-down cycles on a hot Pi, see ``cycle.py`` instead, which
reuses the argument definitions and recording logic below.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time

from ..preflight import format_findings, run_preflight
from ..session import Session
from ..sysmon import SystemMonitor
from .pipeline import Recorder
from .source import FakeCameraSource, IntervalSource

# -- shared argument definitions, reused by cycle.py so both tools stay in sync ----------


def add_camera_args(parser) -> None:
    parser.add_argument("--source", choices=["fake", "picamera2"], default="fake")
    parser.add_argument("--fps", type=float, default=30.0)
    parser.add_argument("--interval-s", type=float, default=None,
                        help="keep only one frame every this many seconds, discarding the rest "
                             "before encoding -- for a calibration shoot: move the checkerboard, "
                             "wait, repeat. The camera still runs at --fps internally; this just "
                             "samples it. Automatically raises --stall-timeout so normal gaps "
                             "between kept frames aren't mistaken for a stalled camera")
    cam = parser.add_argument_group("camera options (picamera2 only)")
    cam.add_argument("--width", type=int, default=1640)
    cam.add_argument("--height", type=int, default=1232)
    cam.add_argument("--quality", type=int, default=90, help="JPEG quality 1-100")
    cam.add_argument("--sensor-mode", type=int, default=None,
                     help="sensor mode index from camera_probe; set it for calibration-critical runs")
    cam.add_argument("--exposure-us", type=int, default=None,
                     help="fixed exposure time in microseconds (turns auto-exposure off)")
    cam.add_argument("--gain", type=float, default=None,
                     help="fixed analogue gain (turns auto-exposure off)")
    cam.add_argument("--encode-workers", type=int, default=None,
                     help="JPEG encoder threads; 0 = encode in the capture thread. Default: 3 for picamera2")
    cam.add_argument("--camera-queue", choices=["default", "on", "off"], default="default",
                     help="off: Picamera2 recycles old frames itself, so a slow consumer cannot starve the camera")
    cam.add_argument("--encoder", choices=["auto", "simplejpeg", "pillow"], default="auto",
                     help="JPEG encoder; auto = simplejpeg if installed, else Pillow (much slower)")
    cam.add_argument("--encode-nice", type=int, default=10,
                     help="nice value for encoder threads so the camera wins CPU contention (0 = off)")
    cam.add_argument("--rotate", type=int, choices=[0, 90, 180, 270], default=0,
                     help="rotate saved frames clockwise by this many degrees. NOT RECOMMENDED for "
                          "recording on the Pi 3B: 90/270 measured at 130ms/frame extra (a memory-layout "
                          "cost, not the rotation logic), over 3x the JPEG encode itself, and took a "
                          "clean run to 80%% dropped frames. Rotate in Phase 2 (AWS) instead. The "
                          "manifest's camera.saved_size records the real saved dimensions, which 90/270 "
                          "swaps for a non-square --width/--height")


def add_recorder_args(parser) -> None:
    parser.add_argument("--queue-size", type=int, default=256,
                        help="frames waiting for the disk; a full queue drops frames. 256 frames is about "
                             "8 s at 30 fps (about 40 MB at 160 KB/frame): enough to hide the SD card's "
                             "multi-second pauses. More frames lost if power is cut")
    parser.add_argument("--fsync-files", action="store_true", help="fsync every frame file")
    parser.add_argument("--stall-timeout", type=float, default=5.0,
                        help="end the run if no frame arrives for this many seconds (0 = off)")
    parser.add_argument("--csv-fsync-interval", type=float, default=1.0,
                        help="seconds between fsyncs of the frame ledger (and system.csv). Each fsync "
                             "forces a filesystem journal commit; larger values lose more rows in a "
                             "power cut but may avoid multi-second write stalls on a slow card")
    parser.add_argument("--no-preflight", action="store_true",
                        help="skip the memory/temperature/power check before recording")
    parser.add_argument("--no-sysmon", action="store_true",
                        help="do not log memory/CPU/disk state to system.csv each second")


def resolve_encode_workers(args) -> None:
    """Fill in --encode-workers' real default, which depends on --source."""
    if args.encode_workers is None:
        args.encode_workers = 3 if args.source == "picamera2" else 0


def build_source(args):
    if args.source == "fake":
        source = FakeCameraSource(fps=args.fps)
    else:
        try:
            import picamera2  # noqa: F401  (checked up front so no empty session is left behind)
        except ImportError:
            raise SystemExit(
                "picamera2 is not installed. On the Pi run: sudo apt install -y python3-picamera2\n"
                "and use the system python3 (a virtualenv must be created with --system-site-packages)."
            )
        from .camera import PicameraSource

        source = PicameraSource(
            width=args.width,
            height=args.height,
            fps=args.fps,
            quality=args.quality,
            sensor_mode=args.sensor_mode,
            exposure_us=args.exposure_us,
            analogue_gain=args.gain,
            encoder=args.encoder,
            rotate_deg=args.rotate,
            queue={"default": None, "on": True, "off": False}[args.camera_queue],
        )
    if args.interval_s:
        source = IntervalSource(source, args.interval_s)
    return source


def resolve_stall_timeout(args) -> float | None:
    """--stall-timeout's effective value: at least 3x --interval-s when both are
    set, so the normal gap between kept calibration shots is never mistaken for
    a stalled camera. An explicit --stall-timeout larger than that floor wins;
    --stall-timeout 0 (off) is left off."""
    stall_timeout = args.stall_timeout or None
    if args.interval_s and stall_timeout is not None:
        stall_timeout = max(stall_timeout, args.interval_s * 3)
    return stall_timeout


def make_recorder(session: Session, source, args) -> Recorder:
    return Recorder(
        session,
        source,
        queue_size=args.queue_size,
        fsync_files=args.fsync_files,
        encode_workers=args.encode_workers,
        stall_timeout_s=resolve_stall_timeout(args),
        encode_nice=args.encode_nice or None,
        fsync_interval_s=args.csv_fsync_interval,
    )


def run_preflight_for(session: Session, out_dir, skip: bool = False) -> None:
    """Print the preflight check and save it on the session manifest, unless skipped."""
    if skip:
        return
    info, findings = run_preflight(disk_path=out_dir if os.path.isdir(out_dir) else ".")
    print(format_findings(info, findings), flush=True)
    session.manifest["preflight"] = {"values": info, "findings": [f"{l}: {m}" for l, m in findings]}
    session.flush()


def start_sysmon(session: Session, recorder: Recorder, args, skip: bool = False):
    if skip:
        return None
    monitor = SystemMonitor(
        session.root / "system.csv",
        fsync_interval_s=max(5.0, args.csv_fsync_interval),
        extra=lambda: {"captured": recorder.captured, "written": recorder.written,
                       "backlog": recorder.queue_depth},
    )
    monitor.start()
    session.set_stream("system", csv="system.csv", interval_s=monitor.interval_s)
    return monitor


def status_loop(recorder: Recorder, every_s: float, stop: threading.Event) -> None:
    """Print a one-line status every ``every_s`` seconds until ``stop`` is set."""
    t0 = last_t = time.monotonic()
    last_written = 0
    while not stop.wait(every_s):
        now = time.monotonic()
        written = recorder.written
        fps = (written - last_written) / (now - last_t)
        if recorder.source_start_s is None:
            state = "camera starting..."
        elif recorder.seconds_since_frame is None:
            state = f"camera started in {recorder.source_start_s}s, NO FRAME YET"
        else:
            state = f"last frame {recorder.seconds_since_frame:.1f}s ago"
        print(
            f"t={now - t0:6.1f}s  captured={recorder.captured} written={written} fps={fps:5.1f}  "
            f"dropped: encode={recorder.dropped_encode} queue={recorder.dropped}  "
            f"backlog={recorder.queue_depth}/{recorder.queue_capacity}  [{state}]",
            flush=True,
        )
        last_t, last_written = now, written


def record_one(source, out_dir, args, duration=None, status_every: float = 5.0):
    """Record one session with ``source`` into ``out_dir``. Used by both this CLI and cycle.py.

    ``duration`` overrides ``args.duration`` — cycle.py passes a fixed chunk length here
    while reusing one parsed ``args`` (and one camera) across every chunk. Returns
    ``(session, summary)``.
    """
    session = Session.create(out_dir)
    run_preflight_for(session, out_dir, skip=args.no_preflight)
    recorder = make_recorder(session, source, args)

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: recorder.request_stop())
    if hasattr(signal, "SIGHUP"):
        # A lost SSH session (e.g. driving out of Wi-Fi range) sends SIGHUP to whatever
        # is attached to it; Python's default reaction is to terminate. Ignore it, so a
        # recording started over SSH keeps running for its full duration after the
        # connection drops. This does NOT make it safe to just background the command
        # over a plain SSH session: once the session dies, print() to its now-broken
        # stdout pipe can itself raise. Launch with output redirected to a real file
        # (`nohup ... > out.log 2>&1 &`), not left attached to the SSH pipe.
        signal.signal(signal.SIGHUP, signal.SIG_IGN)
    if duration is not None:
        timer = threading.Timer(duration, recorder.request_stop)
        timer.daemon = True
        timer.start()
    stop_status = threading.Event()
    if status_every > 0:
        threading.Thread(target=status_loop, args=(recorder, status_every, stop_status), daemon=True).start()

    monitor = start_sysmon(session, recorder, args, skip=args.no_sysmon)
    print(f"session: {session.root}", flush=True)
    summary = recorder.run()
    stop_status.set()
    if monitor is not None:
        monitor.stop()
    return session, summary


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Record frames into a session.")
    add_camera_args(parser)
    add_recorder_args(parser)
    parser.add_argument("--out", default="sessions", help="directory holding sessions")
    parser.add_argument("--duration", type=float, default=None, help="seconds; default: until Ctrl-C")
    parser.add_argument("--status-every", type=float, default=5.0,
                        help="seconds between status lines (0 = off)")
    args = parser.parse_args(argv)
    resolve_encode_workers(args)

    source = build_source(args)
    session, summary = record_one(source, args.out, args, duration=args.duration, status_every=args.status_every)

    for key, value in summary.items():
        print(f"  {key}: {value}")
    print(f"Check it with: python -m ivision.frames.verify {session.root}")
    return 1 if "error" in summary["stop_reason"] else 0


if __name__ == "__main__":
    sys.exit(main())
