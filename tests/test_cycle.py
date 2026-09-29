import tempfile
import unittest
from pathlib import Path

from ivision.frames import cycle as cy
from ivision.frames import record
from ivision.frames.source import FakeCameraSource
from ivision.frames.verify import verify_session


class PlanChunksTest(unittest.TestCase):
    def test_exact_multiple_gives_no_cooldown_after_the_last_chunk(self):
        # The user's own numbers: 5 min record, 2 min cool, 20 min total.
        chunks = cy.plan_chunks(record_s=300, cool_s=120, total_s=1200)
        self.assertEqual(chunks, [[300, 120], [300, 120], [300, 120], [300, 0]])

    def test_non_multiple_shortens_the_final_chunk_instead_of_overshooting(self):
        chunks = cy.plan_chunks(record_s=300, cool_s=60, total_s=700)
        self.assertEqual(chunks, [[300, 60], [300, 60], [100, 0]])
        self.assertEqual(sum(c for c, _ in chunks), 700)

    def test_total_shorter_than_one_chunk_gives_a_single_shortened_chunk(self):
        self.assertEqual(cy.plan_chunks(300, 60, 90), [[90, 0]])

    def test_zero_or_negative_inputs_give_no_chunks(self):
        self.assertEqual(cy.plan_chunks(0, 60, 300), [])
        self.assertEqual(cy.plan_chunks(300, 60, 0), [])
        self.assertEqual(cy.plan_chunks(-1, 60, 300), [])

    def test_zero_cooldown_is_allowed(self):
        self.assertEqual(cy.plan_chunks(100, 0, 250), [[100, 0], [100, 0], [50, 0]])


class FakeArgs:
    """Just enough of parsed argparse Namespace for record_one()."""

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


class RunCycleTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def make_source(self, num_frames_per_chunk=90, fps=30.0):
        # realtime=True, start_ns left as None (paced from "now"), and a generous
        # num_frames: each chunk's duration timer, not the frame count, is what actually
        # stops it. (start_ns=0 would pace against an ancient timestamp and emit every
        # frame instantly, defeating the pacing entirely.)
        return FakeCameraSource(fps=fps, num_frames=num_frames_per_chunk * 10, realtime=True)

    def test_runs_the_planned_chunks_each_as_its_own_verified_session(self):
        source = self.make_source()
        args = FakeArgs()
        results = cy.run_cycle(source, self.base, args, record_s=0.3, cool_s=0.1, total_s=0.9,
                               status_every=0)
        self.assertEqual(len(results), 3)  # 0.9 / 0.3 exactly
        session_dirs = {s.root for s, _ in results}
        self.assertEqual(len(session_dirs), 3)  # three distinct sessions
        for session, summary in results:
            self.assertEqual(summary["stop_reason"], "requested")
            self.assertGreater(summary["frames_written"], 0)
            report = verify_session(session.root)
            self.assertTrue(report.ok, report.format())

    def test_sleeps_between_chunks_but_not_after_the_last_one(self):
        sleeps = []
        source = self.make_source()
        args = FakeArgs()
        cy.run_cycle(source, self.base, args, record_s=0.2, cool_s=0.15, total_s=0.4,
                     status_every=0, sleep=sleeps.append)
        self.assertEqual(sleeps, [0.15])  # 0.4 / 0.2 = 2 chunks -> 1 cooldown between them, none after

    def test_camera_is_closed_after_the_last_chunk(self):
        closed = []
        source = self.make_source()
        source.close = lambda: closed.append(True)
        cy.run_cycle(source, self.base, FakeArgs(), record_s=0.2, cool_s=0.05, total_s=0.2, status_every=0)
        # Each chunk's own Recorder already closes the source (close() is idempotent), and
        # run_cycle's own safety-net close adds at least one more call; what matters is that
        # it happened at least once, not the exact count.
        self.assertTrue(closed, "camera close() should have been attempted")

    def test_stops_early_on_a_chunk_error_and_still_closes_the_camera(self):
        closed = []

        class DiesOnce(FakeCameraSource):
            def __init__(self, *a, **kw):
                super().__init__(*a, **kw)
                self.calls = 0

            def frames(self):
                self.calls += 1
                if self.calls == 2:
                    raise RuntimeError("camera fell over")
                yield from super().frames()

        source = DiesOnce(fps=30, num_frames=1000, realtime=True, start_ns=0)
        source.close = lambda: closed.append(True)
        sleeps = []
        results = cy.run_cycle(source, self.base, FakeArgs(), record_s=0.2, cool_s=0.05, total_s=1.0,
                               status_every=0, sleep=sleeps.append)
        self.assertEqual(len(results), 2)  # chunk 1 ok, chunk 2 errors, chunk 3+ never run
        self.assertIn("source_error", results[1][1]["stop_reason"])
        self.assertEqual(sleeps, [0.05])  # cooldown after chunk 1 happened; none after the failure
        self.assertTrue(closed, "camera close() should have been attempted")

    def test_no_chunks_raises_a_clear_error(self):
        with self.assertRaises(ValueError):
            cy.run_cycle(self.make_source(), self.base, FakeArgs(), record_s=0, cool_s=1, total_s=10)

    def test_each_chunk_ledger_restarts_at_seq_zero(self):
        # Every chunk is an independent session, like any other recording; seq is not
        # continued across chunks (the timestamp in each frame's filename is what a reader
        # uses to place chunks on a shared timeline, not a shared seq counter).
        source = self.make_source()
        results = cy.run_cycle(source, self.base, FakeArgs(), record_s=0.2, cool_s=0.05, total_s=0.5,
                               status_every=0)
        for session, _ in results:
            first_row = session.frames_csv.read_text().splitlines()[1]
            self.assertEqual(first_row.split(",")[0], "0")


class CliArgsTest(unittest.TestCase):
    def test_shares_camera_and_recorder_arguments_with_record_py(self):
        import argparse

        parser = argparse.ArgumentParser()
        record.add_camera_args(parser)
        record.add_recorder_args(parser)
        parser.add_argument("--record-minutes", type=float, required=True)
        parser.add_argument("--cool-minutes", type=float, required=True)
        parser.add_argument("--total-minutes", type=float, required=True)
        args = parser.parse_args(["--source", "picamera2", "--width", "1280", "--height", "960",
                                  "--encode-workers", "2", "--record-minutes", "5",
                                  "--cool-minutes", "2", "--total-minutes", "20"])
        self.assertEqual((args.width, args.height, args.encode_workers), (1280, 960, 2))

    def test_rejects_non_positive_record_or_total_minutes(self):
        with self.assertRaises(SystemExit):
            cy.main(["--record-minutes", "0", "--cool-minutes", "1", "--total-minutes", "10"])
        with self.assertRaises(SystemExit):
            cy.main(["--record-minutes", "5", "--cool-minutes", "1", "--total-minutes", "0"])


if __name__ == "__main__":
    unittest.main()
