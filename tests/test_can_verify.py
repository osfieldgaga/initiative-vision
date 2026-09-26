import tempfile
import threading
import unittest
from pathlib import Path

from ivision.can.pipeline import CanRecorder
from ivision.can.source import FakeCanSource
from ivision.can.verify import verify_session
from ivision.session import Session


def record(base, num_frames=50, queue_size=4096, source=None, **kw):
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


class VerifyTest(TmpDirCase):
    def make(self, n=50):
        session, _ = record(self.base, num_frames=n)
        return session

    def test_clean_run_passes_with_expected_stats(self):
        s = self.make(50)
        r = verify_session(s.root)
        self.assertTrue(r.ok, r.format())
        self.assertEqual(r.stats["rows"], 50)
        self.assertEqual(r.stats["seq_gaps"], 0)
        self.assertEqual(r.stats["distinct_ids"], 3)  # FakeCanSource's default 3 messages
        self.assertIn("most_frequent_ids", r.stats)
        self.assertAlmostEqual(r.stats["achieved_hz"], 100.0, delta=1.0)

    def test_missing_can_csv_is_an_error(self):
        s = self.make(5)
        s.root.joinpath("can.csv").unlink()
        r = verify_session(s.root)
        self.assertFalse(r.ok)
        self.assertTrue(any("can.csv not found" in e for e in r.errors))

    def test_non_monotonic_seq_is_an_error(self):
        s = self.make(20)
        lines = (s.root / "can.csv").read_text().splitlines(keepends=True)
        lines[5], lines[6] = lines[6], lines[5]
        (s.root / "can.csv").write_text("".join(lines))
        r = verify_session(s.root)
        self.assertFalse(r.ok)
        self.assertTrue(any("not increasing" in e for e in r.errors))

    def test_torn_final_row_is_only_a_warning_after_unclean_stop(self):
        s = self.make(20)
        s.manifest["status"] = "recording"
        s.manifest["stopped"] = None
        s.flush()
        path = s.root / "can.csv"
        path.write_text(path.read_text()[:-5])  # cut mid-row
        r = verify_session(s.root)
        self.assertTrue(r.ok, r.format())
        self.assertTrue(any("torn" in w for w in r.warnings))
        self.assertTrue(any("not closed cleanly" in w for w in r.warnings))

    def test_manifest_drop_count_mismatch_is_an_error(self):
        s = self.make(20)
        s.manifest["streams"]["can"]["can_frames_dropped_queue"] = 7
        s.flush()
        r = verify_session(s.root)
        self.assertFalse(r.ok)
        self.assertTrue(any("accounting mismatch" in e for e in r.errors))

    def test_trailing_drops_are_a_warning_via_the_manifest_not_seq_gaps(self):
        # A small queue alone isn't enough to force a drop when the writer keeps up
        # easily -- gate the writer so the queue genuinely backs up, same pattern as
        # test_can_pipeline.py's slow-writer test. All drops land at the very end of
        # the run here (the source stops before the gate opens), which is exactly the
        # case seq_gaps alone can't see -- there's no later row to gap against.
        gate = threading.Event()
        session = Session.create(self.base)
        source = FakeCanSource(hz=1000, num_frames=200, realtime=False, start_ns=0)
        recorder = CanRecorder(session, source, queue_size=4, flush_interval_s=0.02)
        orig_write_row = recorder._csv.write_row

        def slow_write_row(values):
            gate.wait(timeout=5)
            orig_write_row(values)

        recorder._csv.write_row = slow_write_row
        t = threading.Thread(target=recorder.run)
        t.start()
        for _ in range(500):
            if recorder.captured >= 200:
                break
            threading.Event().wait(0.01)
        gate.set()
        t.join(timeout=10)

        r = verify_session(session.root)
        self.assertTrue(r.ok, r.format())
        self.assertGreater(session.manifest["streams"]["can"]["can_frames_dropped_queue"], 0)
        self.assertEqual(r.stats["seq_gaps"], 0)  # confirms this really is the trailing-only case
        self.assertTrue(any("were dropped" in w for w in r.warnings))

    def test_interior_drops_show_up_as_seq_gaps_too(self):
        # Same gate mechanism, but resumed (not left stopped) so capture continues
        # after the backlog clears -- creates a real gap in the middle of the ledger.
        gate = threading.Event()
        at_50 = threading.Event()
        resume = threading.Event()

        class Pausing(FakeCanSource):
            def frames(self):
                for i, f in enumerate(super().frames()):
                    if i == 50:
                        at_50.set()
                        resume.wait(timeout=5)
                    yield f

        session = Session.create(self.base)
        source = Pausing(hz=1000, num_frames=80, realtime=False, start_ns=0)
        recorder = CanRecorder(session, source, queue_size=4, flush_interval_s=0.02)
        orig_write_row = recorder._csv.write_row

        def slow_write_row(values):
            gate.wait(timeout=5)
            orig_write_row(values)

        recorder._csv.write_row = slow_write_row
        t = threading.Thread(target=recorder.run)
        t.start()
        self.assertTrue(at_50.wait(timeout=5))
        gate.set()  # let the backlog drain before capture resumes
        for _ in range(500):
            if recorder._queue.qsize() == 0:
                break
            threading.Event().wait(0.01)
        resume.set()
        t.join(timeout=10)

        r = verify_session(session.root)
        self.assertTrue(r.ok, r.format())
        self.assertGreater(r.stats["seq_gaps"], 0)
        self.assertTrue(any("were dropped" in w for w in r.warnings))

    def test_no_interface_means_no_if_stats_warnings(self):
        # FakeCanSource has no `interface` -- if_stats_before/after are None.
        s = self.make(10)
        r = verify_session(s.root)
        self.assertTrue(r.ok, r.format())
        self.assertNotIn("interface_rx_packets", r.stats)

    def test_a_long_gap_between_frames_is_neither_an_error_nor_a_warning(self):
        # Unlike frames/verify.py, a quiet bus must never be flagged as a stall.
        source = FakeCanSource(hz=1000, num_frames=3, realtime=False, start_ns=0)
        session, _ = record(self.base, source=source)
        # Manually stretch the last timestamp to simulate a multi-hour silent gap.
        lines = (session.root / "can.csv").read_text().splitlines()
        header, rows = lines[0], lines[1:]
        last = rows[-1].split(",")
        last[1] = str(int(last[1]) + 4 * 3600 * 1_000_000_000)  # +4 hours
        rows[-1] = ",".join(last)
        (session.root / "can.csv").write_text("\n".join([header, *rows]) + "\n")
        r = verify_session(session.root)
        self.assertTrue(r.ok, r.format())
        self.assertFalse(any("stall" in w.lower() for w in r.warnings))
        self.assertGreater(r.stats["longest_gap_s"], 3600)

    def test_kernel_interface_drops_reported_as_a_warning(self):
        s = self.make(10)
        s.manifest["streams"]["can"]["if_stats_before"] = {
            "rx_packets": 100, "rx_dropped": 0, "rx_errors": 0, "rx_over_errors": 0, "rx_fifo_errors": 0,
        }
        s.manifest["streams"]["can"]["if_stats_after"] = {
            "rx_packets": 100 + s.manifest["streams"]["can"]["can_frames_captured"],
            "rx_dropped": 5, "rx_errors": 0, "rx_over_errors": 0, "rx_fifo_errors": 0,
        }
        s.flush()
        r = verify_session(s.root)
        self.assertTrue(r.ok, r.format())
        self.assertTrue(any("kernel/interface-level loss" in w and "rx_dropped +5" in w for w in r.warnings))

    def test_untracked_socket_loss_is_detected_and_reported(self):
        s = self.make(10)
        captured = s.manifest["streams"]["can"]["can_frames_captured"]
        # Interface saw far more than we captured, and rx_dropped doesn't explain the gap.
        s.manifest["streams"]["can"]["if_stats_before"] = {
            "rx_packets": 0, "rx_dropped": 0, "rx_errors": 0, "rx_over_errors": 0, "rx_fifo_errors": 0,
        }
        s.manifest["streams"]["can"]["if_stats_after"] = {
            "rx_packets": captured + 100, "rx_dropped": 0, "rx_errors": 0, "rx_over_errors": 0, "rx_fifo_errors": 0,
        }
        s.flush()
        r = verify_session(s.root)
        self.assertTrue(r.ok, r.format())
        self.assertEqual(r.stats["untracked_socket_loss"], 100)
        self.assertTrue(any("never captured by this process" in w for w in r.warnings))


if __name__ == "__main__":
    unittest.main()
