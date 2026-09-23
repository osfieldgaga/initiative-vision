import errno
import tempfile
import threading
import unittest
from pathlib import Path

from ivision.can.pipeline import CanRecorder, read_if_stats
from ivision.can.source import FakeCanSource
from ivision.session import Session, load_manifest


def record(base, num_frames=300, queue_size=4096, source=None, **kw):
    source = source or FakeCanSource(hz=100, num_frames=num_frames, realtime=False, start_ns=1_000_000_000)
    session = Session.create(base)
    recorder = CanRecorder(session, source, queue_size=queue_size, flush_interval_s=0.02, **kw)
    summary = recorder.run()
    return session, summary


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class CanPipelineTest(TmpDirCase):
    def test_every_frame_gets_a_row(self):
        session, summary = record(self.base, num_frames=300)
        self.assertEqual(summary["stop_reason"], "source_ended")
        self.assertEqual(summary["can_frames_captured"], 300)
        self.assertEqual(summary["can_frames_written"], 300)
        self.assertEqual(summary["can_frames_dropped_queue"], 0)
        rows = (session.root / "can.csv").read_text().splitlines()
        self.assertEqual(len(rows), 301)  # header + 300

    def test_ledger_format(self):
        session, _ = record(self.base, num_frames=1,
                            source=FakeCanSource(hz=100, num_frames=1, realtime=False, start_ns=1_000_000_000,
                                                  messages=[(0x1F1, b"\xAA\xBB")]))
        lines = (session.root / "can.csv").read_text().splitlines()
        self.assertEqual(lines[0], "seq,timestamp_ns,kernel_realtime_ns,can_id,is_extended,is_remote_frame,dlc,data")
        self.assertEqual(lines[1], "1,1000000000,,0x1F1,0,0,2,aabb")

    def test_slow_writer_drops_frames_but_never_blocks_and_accounts_exactly(self):
        gate = threading.Event()

        session = Session.create(self.base)
        source = FakeCanSource(hz=1000, num_frames=200, realtime=False, start_ns=0)
        recorder = CanRecorder(session, source, queue_size=4, flush_interval_s=0.02)

        # Stall the writer by blocking the csv module's write_row via monkeypatch
        orig_write_row = recorder._csv.write_row

        def slow_write_row(values):
            gate.wait(timeout=5)
            orig_write_row(values)

        recorder._csv.write_row = slow_write_row

        t = threading.Thread(target=lambda: setattr(self, "summary", recorder.run()))
        t.start()
        deadline = 5
        while recorder.captured < 200 and deadline > 0:
            threading.Event().wait(0.01)
            deadline -= 0.01
        self.assertEqual(recorder.captured, 200)
        gate.set()
        t.join(timeout=10)
        s = self.summary
        self.assertGreater(s["can_frames_dropped_queue"], 0)
        self.assertEqual(s["can_frames_written"] + s["can_frames_dropped_queue"], s["can_frames_captured"])

    def test_source_crash_is_reported_not_treated_as_clean_end(self):
        class Crashing(FakeCanSource):
            def frames(self):
                for i, f in enumerate(super().frames()):
                    if i == 5:
                        raise RuntimeError("bus fell off")
                    yield f

        source = Crashing(hz=100, num_frames=100, realtime=False, start_ns=0)
        session, summary = record(self.base, source=source)
        self.assertTrue(summary["stop_reason"].startswith("source_error"), summary)
        self.assertEqual(summary["can_frames_written"], 5)

    def test_requested_stop_finishes_queued_frames_and_closes_cleanly(self):
        source = FakeCanSource(hz=200, realtime=True)  # endless
        session = Session.create(self.base)
        recorder = CanRecorder(session, source, flush_interval_s=0.02)
        threading.Timer(0.3, recorder.request_stop).start()
        summary = recorder.run()
        self.assertEqual(summary["stop_reason"], "requested")
        self.assertGreater(summary["can_frames_written"], 5)
        self.assertEqual(load_manifest(session.root)["status"], "complete")

    def test_silence_never_ends_the_run_on_its_own(self):
        """The defining difference from frames: a quiet bus is not a fault.
        A source that goes quiet for well past any frame-pipeline stall
        timeout must still be running until explicitly stopped."""
        source = FakeCanSource(hz=200, num_frames=5, realtime=False, start_ns=0)
        session = Session.create(self.base)
        recorder = CanRecorder(session, source, flush_interval_s=0.02)
        t = threading.Thread(target=lambda: setattr(self, "summary", recorder.run()))
        t.start()
        t.join(timeout=2)
        # The source ran out of frames (5 total) almost instantly (realtime=False),
        # but nothing here should have called it an error -- it's a clean end.
        self.assertFalse(t.is_alive())
        self.assertEqual(self.summary["stop_reason"], "source_ended")
        self.assertEqual(self.summary["can_frames_written"], 5)

    def test_can_manifest_records_source_config(self):
        session, _ = record(self.base, num_frames=2)
        can = load_manifest(session.root)["streams"]["can"]
        self.assertEqual(can["kind"], "fake")
        self.assertEqual(can["status"], "complete")

    def test_if_stats_are_none_for_a_source_with_no_interface(self):
        session, summary = record(self.base, num_frames=2)
        self.assertIsNone(summary["if_stats_before"])
        self.assertIsNone(summary["if_stats_after"])


class ReadIfStatsTest(unittest.TestCase):
    def test_missing_interface_returns_none(self):
        self.assertIsNone(read_if_stats("no_such_interface_xyz"))

    def test_loopback_interface_has_countable_stats(self):
        # "lo" exists on every Linux box this runs on, unlike vcan0.
        stats = read_if_stats("lo")
        if stats is None:
            self.skipTest("no /sys/class/net/lo on this machine")
        self.assertIn("rx_packets", stats)
        self.assertIsInstance(stats["rx_packets"], int)


if __name__ == "__main__":
    unittest.main()
