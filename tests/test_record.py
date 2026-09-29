import argparse
import os
import signal
import tempfile
import threading
import time
import unittest
from pathlib import Path

from ivision.frames import record
from ivision.frames.source import FakeCameraSource


class FakeArgs:
    """Just enough of a parsed argparse Namespace for record_one()."""

    def __init__(self, **kw):
        self.no_preflight = True
        self.no_sysmon = True
        self.queue_size = 64
        self.fsync_files = False
        self.encode_workers = 0
        self.stall_timeout = 5.0
        self.interval_s = None
        self.live_preview = False
        self.encode_nice = 10
        self.csv_fsync_interval = 1.0
        self.__dict__.update(kw)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


@unittest.skipUnless(hasattr(signal, "SIGHUP"), "SIGHUP is POSIX-only")
class SighupTest(TmpCase):
    """signal.signal() only works from the main thread, so record_one() is called directly
    here (not from a background thread); a background Timer sends the signal instead."""

    def setUp(self):
        super().setUp()
        self._old_sighup = signal.getsignal(signal.SIGHUP)
        self.addCleanup(signal.signal, signal.SIGHUP, self._old_sighup)

    def test_sighup_does_not_end_the_recording_early(self):
        # Regression motivation (2026-09-22): a recording started over SSH and left
        # running while driving out of Wi-Fi range must not die when the SSH session's
        # SIGHUP arrives; it should keep going for its full requested duration. A weaker
        # assertion (e.g. "some frames were written") wouldn't tell an early stop from a
        # full one apart, since both go through request_stop() and report "requested";
        # timing and frame count against the 0.1s-early-signal vs 0.6s-full-duration gap
        # are what actually distinguish them.
        source = FakeCameraSource(fps=30, realtime=True)  # endless until stopped
        threading.Timer(0.1, lambda: os.kill(os.getpid(), signal.SIGHUP)).start()
        t0 = time.monotonic()
        _, summary = record.record_one(source, self.base, FakeArgs(), duration=0.6, status_every=0)
        elapsed = time.monotonic() - t0

        self.assertEqual(summary["stop_reason"], "requested")  # the duration timer, not the signal
        self.assertGreaterEqual(elapsed, 0.5)  # ran to ~0.6s, not cut short at the ~0.1s signal
        self.assertGreaterEqual(summary["frames_written"], 12)  # ~18 expected at 30fps*0.6s; ~3 if cut short

    def test_sighup_handler_installed_is_sig_ign(self):
        source = FakeCameraSource(fps=30, num_frames=3, realtime=False, start_ns=0)
        record.record_one(source, self.base, FakeArgs(), duration=None, status_every=0)
        self.assertEqual(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)


class SharedArgsTest(unittest.TestCase):
    """add_camera_args/add_recorder_args are reused by cycle.py; keep their contract stable."""

    def parse(self, argv):
        parser = argparse.ArgumentParser()
        record.add_camera_args(parser)
        record.add_recorder_args(parser)
        return parser.parse_args(argv)

    def test_defaults(self):
        args = self.parse([])
        self.assertEqual(args.source, "fake")
        self.assertEqual((args.width, args.height, args.quality), (1640, 1232, 90))
        self.assertIsNone(args.encode_workers)
        self.assertEqual(args.queue_size, 256)
        self.assertEqual(args.csv_fsync_interval, 1.0)
        self.assertEqual(args.rotate, 0)
        self.assertIsNone(args.interval_s)

    def test_rotate_accepts_only_right_angles(self):
        for value in (0, 90, 180, 270):
            self.assertEqual(self.parse(["--rotate", str(value)]).rotate, value)
        with self.assertRaises(SystemExit):
            self.parse(["--rotate", "45"])

    def test_build_source_passes_rotate_deg_through_to_picamera_source(self):
        import sys
        import types
        from unittest import mock

        args = self.parse(["--source", "picamera2", "--rotate", "90"])
        fake_picamera2 = types.ModuleType("picamera2")  # this desktop has no real picamera2
        with mock.patch.dict(sys.modules, {"picamera2": fake_picamera2}), \
                mock.patch("ivision.frames.camera.PicameraSource") as MockSource:
            record.build_source(args)
        self.assertEqual(MockSource.call_args.kwargs["rotate_deg"], 90)

    def test_resolve_encode_workers_depends_on_source(self):
        fake = self.parse(["--source", "fake"])
        record.resolve_encode_workers(fake)
        self.assertEqual(fake.encode_workers, 0)

        pi = self.parse(["--source", "picamera2"])
        record.resolve_encode_workers(pi)
        self.assertEqual(pi.encode_workers, 3)

        explicit = self.parse(["--source", "picamera2", "--encode-workers", "2"])
        record.resolve_encode_workers(explicit)
        self.assertEqual(explicit.encode_workers, 2)  # an explicit value is never overridden

    def test_build_source_wraps_in_interval_source_when_set(self):
        from ivision.frames.source import IntervalSource

        args = self.parse(["--source", "fake", "--interval-s", "5"])
        source = record.build_source(args)
        self.assertIsInstance(source, IntervalSource)
        self.assertEqual(source.interval_s, 5.0)

    def test_build_source_no_wrapping_when_interval_s_unset(self):
        from ivision.frames.source import IntervalSource

        args = self.parse(["--source", "fake"])
        source = record.build_source(args)
        self.assertNotIsInstance(source, IntervalSource)

    def test_resolve_stall_timeout_raised_for_a_long_interval(self):
        args = self.parse(["--interval-s", "5"])  # default --stall-timeout is 5.0
        self.assertEqual(record.resolve_stall_timeout(args), 15.0)  # 3x the interval

    def test_resolve_stall_timeout_keeps_an_explicit_larger_value(self):
        args = self.parse(["--interval-s", "5", "--stall-timeout", "60"])
        self.assertEqual(record.resolve_stall_timeout(args), 60.0)

    def test_resolve_stall_timeout_off_stays_off_even_with_interval(self):
        args = self.parse(["--interval-s", "5", "--stall-timeout", "0"])
        self.assertIsNone(record.resolve_stall_timeout(args))

    def test_resolve_stall_timeout_unaffected_without_interval(self):
        args = self.parse([])
        self.assertEqual(record.resolve_stall_timeout(args), 5.0)


class RecordOneTest(TmpCase):
    def test_creates_a_session_and_returns_its_summary(self):
        source = FakeCameraSource(fps=30, num_frames=10, realtime=False, start_ns=0)
        session, summary = record.record_one(source, self.base, FakeArgs(), duration=None, status_every=0)
        self.assertEqual(summary["frames_written"], 10)
        self.assertTrue(session.manifest_path.exists())

    def test_preflight_can_be_skipped(self):
        source = FakeCameraSource(fps=30, num_frames=3, realtime=False, start_ns=0)
        session, _ = record.record_one(source, self.base, FakeArgs(no_preflight=True), duration=None,
                                       status_every=0)
        self.assertNotIn("preflight", session.manifest)


if __name__ == "__main__":
    unittest.main()
