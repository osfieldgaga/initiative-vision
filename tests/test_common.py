import json
import os
import tempfile
import unittest
from pathlib import Path

from ivision.clock import read_anchors
from ivision.csvlog import DurableCsvWriter
from ivision.session import Session, load_manifest


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class ClockTest(unittest.TestCase):
    def test_anchors_have_all_clocks_and_bounded_span(self):
        a = read_anchors()
        for key in ("monotonic_ns", "boottime_ns", "realtime_ns", "read_span_ns"):
            self.assertIn(key, a)
            self.assertIsInstance(a[key], int)
        self.assertGreaterEqual(a["read_span_ns"], 0)
        # The reads are back-to-back: the span should be far under a millisecond
        # on any machine that isn't badly overloaded.
        self.assertLess(a["read_span_ns"], 50_000_000)

    def test_boottime_not_behind_monotonic(self):
        a = read_anchors()
        self.assertGreaterEqual(a["boottime_ns"] + 50_000_000, a["monotonic_ns"])


class CsvLogTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def read(self, name="log.csv"):
        return (self.dir / name).read_text()

    def test_header_written_immediately(self):
        w = DurableCsvWriter(self.dir / "log.csv", ["a", "b"])
        self.assertEqual(self.read(), "a,b\n")
        w.close()

    def test_rows_held_until_flush_interval(self):
        clock = FakeClock()
        w = DurableCsvWriter(self.dir / "log.csv", ["a", "b"], flush_interval_s=0.5, clock=clock)
        w.write_row([1, "x"])
        self.assertEqual(self.read(), "a,b\n")  # still buffered
        clock.t = 0.6
        w.tick()
        self.assertEqual(self.read(), "a,b\n1,x\n")
        w.close()

    def test_close_flushes_everything(self):
        w = DurableCsvWriter(self.dir / "log.csv", ["a", "b"])
        for i in range(5):
            w.write_row([i, "f"])
        w.close()
        self.assertEqual(self.read().count("\n"), 6)
        self.assertEqual(w.rows_written, 5)

    def test_fsync_only_on_its_interval(self):
        clock = FakeClock()
        w = DurableCsvWriter(
            self.dir / "log.csv", ["a"], flush_interval_s=0.1, fsync_interval_s=1.0, clock=clock
        )
        calls = []
        w._fsync = lambda: (calls.append(clock.t), setattr(w, "_dirty", False))
        for t in (0.2, 0.4, 0.6):
            clock.t = t
            w.write_row([1])
        self.assertEqual(calls, [])
        clock.t = 1.2
        w.write_row([2])
        self.assertEqual(len(calls), 1)
        w._fsync = lambda: None
        w.close()

    def test_values_needing_quotes_round_trip(self):
        import csv

        w = DurableCsvWriter(self.dir / "log.csv", ["a", "b"])
        w.write_row(['has,comma', 'has"quote'])
        w.close()
        with open(self.dir / "log.csv", newline="") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[1], ['has,comma', 'has"quote'])

    def test_wrong_column_count_rejected(self):
        w = DurableCsvWriter(self.dir / "log.csv", ["a", "b"])
        with self.assertRaises(ValueError):
            w.write_row([1])
        w.close()

    def test_never_overwrites_existing_file(self):
        (self.dir / "log.csv").write_text("precious\n")
        with self.assertRaises(FileExistsError):
            DurableCsvWriter(self.dir / "log.csv", ["a"])
        self.assertEqual(self.read(), "precious\n")

    def test_write_after_close_rejected(self):
        w = DurableCsvWriter(self.dir / "log.csv", ["a"])
        w.close()
        w.close()  # idempotent
        with self.assertRaises(ValueError):
            w.write_row([1])


class SessionTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_create_layout_and_initial_manifest(self):
        s = Session.create(self.base, camera={"target_fps": 30})
        self.assertTrue(s.frames_dir.is_dir())
        m = load_manifest(s.root)
        self.assertEqual(m["status"], "recording")
        self.assertIsNone(m["stopped"])
        self.assertEqual(m["camera"]["calibration"], "uncalibrated")
        self.assertEqual(m["camera"]["target_fps"], 30)
        self.assertIn("realtime_ns", m["started"])

    def test_finalize_marks_complete_and_records_stop_anchors(self):
        s = Session.create(self.base)
        s.set_stream("frames", frames_written=3)
        s.finalize()
        m = load_manifest(s.root)
        self.assertEqual(m["status"], "complete")
        self.assertGreaterEqual(m["stopped"]["monotonic_ns"], m["started"]["monotonic_ns"])
        self.assertEqual(m["streams"]["frames"]["frames_written"], 3)

    def test_session_ids_unique_and_no_reuse(self):
        ids = {Session.create(self.base).manifest["session_id"] for _ in range(20)}
        self.assertEqual(len(ids), 20)
        Session.create(self.base, session_id="fixed")
        with self.assertRaises(FileExistsError):
            Session.create(self.base, session_id="fixed")

    def test_manifest_write_is_atomic_no_tmp_left(self):
        s = Session.create(self.base)
        s.flush()
        # session.json.lock is a legitimate, permanent artifact (see flush()'s
        # docstring -- it's what makes sharing a session across two processes
        # safe), not a leftover; only *.tmp must never survive a flush.
        self.assertEqual(
            sorted(p.name for p in s.root.iterdir()),
            ["frames", "session.json", "session.json.lock"],
        )
        json.loads(s.manifest_path.read_text())  # parses


if __name__ == "__main__":
    unittest.main()
