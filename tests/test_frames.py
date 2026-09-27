import errno
import tempfile
import threading
import unittest
from pathlib import Path

from ivision.frames.pipeline import Recorder, write_frame_file
from ivision.frames.source import FakeCameraSource, synthetic_jpeg
from ivision.frames.verify import verify_session
from ivision.session import Session, load_manifest


def record(base, num_frames=300, queue_size=1000, file_writer=write_frame_file, source=None, **kw):
    source = source or FakeCameraSource(fps=30, num_frames=num_frames, realtime=False, start_ns=1_000_000_000)
    session = Session.create(base)
    recorder = Recorder(session, source, queue_size=queue_size, file_writer=file_writer,
                        flush_interval_s=0.02, **kw)
    summary = recorder.run()
    return session, summary


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class PipelineTest(TmpDirCase):
    def test_every_frame_gets_a_file_and_a_row(self):
        session, summary = record(self.base, num_frames=300)
        self.assertEqual(summary["stop_reason"], "source_ended")
        self.assertEqual(summary["frames_captured"], 300)
        self.assertEqual(summary["frames_written"], 300)
        self.assertEqual(summary["frames_dropped_queue"], 0)
        self.assertEqual(len(list(session.frames_dir.glob("*.jpg"))), 300)
        report = verify_session(session.root)
        self.assertTrue(report.ok, report.format())
        self.assertEqual(report.stats["rows"], 300)
        self.assertEqual(report.stats["seq_gaps"], 0)

    def test_naming_and_ledger_format(self):
        session, _ = record(self.base, num_frames=3)
        lines = session.frames_csv.read_text().splitlines()
        self.assertEqual(lines[0], "seq,filename,timestamp_ns")
        self.assertEqual(lines[1], "0,frames/00000000_1000000000.jpg,1000000000")
        self.assertEqual(lines[2], "1,frames/00000001_1033333333.jpg,1033333333")

    def test_no_tmp_files_left_after_clean_run(self):
        session, _ = record(self.base, num_frames=20)
        self.assertEqual(list(session.frames_dir.glob("*.tmp")), [])

    def test_slow_disk_drops_frames_but_never_blocks_and_accounts_exactly(self):
        gate = threading.Event()

        def slow_writer(path, data, fsync):
            gate.wait(timeout=5)
            write_frame_file(path, data, fsync)

        source = FakeCameraSource(fps=30, num_frames=200, realtime=False, start_ns=0)
        session = Session.create(self.base)
        recorder = Recorder(session, source, queue_size=4, file_writer=slow_writer,
                            flush_interval_s=0.02)
        t = threading.Thread(target=lambda: setattr(self, "summary", recorder.run()))
        t.start()
        # The source finishes all 200 frames while the writer is stalled: capture
        # must not have been blocked by the disk.
        deadline = 5
        while recorder.captured < 200 and deadline > 0:
            threading.Event().wait(0.01)
            deadline -= 0.01
        self.assertEqual(recorder.captured, 200)
        gate.set()
        t.join(timeout=10)
        s = self.summary
        self.assertGreater(s["frames_dropped_queue"], 0)
        self.assertEqual(s["frames_written"] + s["frames_dropped_queue"], s["frames_captured"])
        report = verify_session(session.root)
        self.assertTrue(report.ok, report.format())
        # Drops after the last written frame leave no interior seq gap, but the
        # manifest reconciliation still accounts for every one of them.
        self.assertEqual(report.stats["missing_seq_total"], s["frames_dropped_queue"])

    def test_mid_stream_drops_show_up_as_seq_gaps(self):
        gate = threading.Event()
        at_100 = threading.Event()
        resume = threading.Event()

        def slow_writer(path, data, fsync):
            gate.wait(timeout=5)
            write_frame_file(path, data, fsync)

        class Pausing(FakeCameraSource):
            def frames(self):
                for f in super().frames():
                    if f.seq == 100:
                        at_100.set()
                        resume.wait(timeout=5)
                    yield f

        source = Pausing(fps=30, num_frames=120, realtime=False, start_ns=0)
        session = Session.create(self.base)
        recorder = Recorder(session, source, queue_size=4, file_writer=slow_writer,
                            flush_interval_s=0.02)
        t = threading.Thread(target=lambda: setattr(self, "summary", recorder.run()))
        t.start()
        self.assertTrue(at_100.wait(timeout=5))
        gate.set()  # disk recovers; let the backlog drain before frames resume
        for _ in range(500):
            if recorder._queue.qsize() == 0:
                break
            threading.Event().wait(0.01)
        resume.set()
        t.join(timeout=10)
        s = self.summary
        report = verify_session(session.root)
        self.assertTrue(report.ok, report.format())
        self.assertGreater(report.stats["seq_gaps"], 0)  # the stalled stretch, mid-ledger
        self.assertGreater(s["frames_dropped_queue"], 50)
        self.assertGreater(s["last_seq_written"], 100)  # capture resumed after the gap
        self.assertEqual(s["frames_written"] + s["frames_dropped_queue"], 120)

    def test_queue_depth_shows_the_backlog_while_the_disk_is_stalled(self):
        gate = threading.Event()

        def stalled(path, data, fsync):
            gate.wait(timeout=5)
            write_frame_file(path, data, fsync)

        source = FakeCameraSource(fps=30, num_frames=30, realtime=False, start_ns=0)
        session = Session.create(self.base)
        recorder = Recorder(session, source, queue_size=16, file_writer=stalled, flush_interval_s=0.02)
        self.assertEqual((recorder.queue_depth, recorder.queue_capacity), (0, 16))
        t = threading.Thread(target=recorder.run)
        t.start()
        for _ in range(500):
            if recorder.captured >= 30:
                break
            threading.Event().wait(0.01)
        self.assertGreaterEqual(recorder.queue_depth, 14)  # full or nearly (writer holds one)
        gate.set()
        t.join(timeout=10)
        self.assertEqual(recorder.queue_depth, 0)

    def test_disk_full_stops_cleanly_and_says_why(self):
        writes = {"n": 0}

        def failing_writer(path, data, fsync):
            writes["n"] += 1
            if writes["n"] > 50:
                raise OSError(errno.ENOSPC, "No space left on device")
            write_frame_file(path, data, fsync)

        session, summary = record(self.base, num_frames=500, file_writer=failing_writer)
        self.assertTrue(summary["stop_reason"].startswith("storage_error"), summary)
        self.assertIn("No space left on device", summary["stop_reason"])
        self.assertEqual(summary["frames_written"], 50)
        m = load_manifest(session.root)
        self.assertEqual(m["status"], "complete")
        # Everything logged before the failure is intact.
        report = verify_session(session.root)
        self.assertTrue(report.ok, report.format())
        self.assertEqual(report.stats["rows"], 50)

    def test_source_crash_is_reported_not_treated_as_clean_end(self):
        class Crashing(FakeCameraSource):
            def frames(self):
                for i, f in enumerate(super().frames()):
                    if i == 5:
                        raise RuntimeError("sensor fell off the bus")
                    yield f

        source = Crashing(fps=30, num_frames=100, realtime=False, start_ns=0)
        session, summary = record(self.base, source=source)
        self.assertTrue(summary["stop_reason"].startswith("source_error"), summary)
        self.assertEqual(summary["frames_written"], 5)
        self.assertTrue(verify_session(session.root).ok)

    def test_requested_stop_finishes_queued_frames_and_closes_cleanly(self):
        source = FakeCameraSource(fps=200, realtime=True)  # endless
        session = Session.create(self.base)
        recorder = Recorder(session, source, flush_interval_s=0.02)
        threading.Timer(0.3, recorder.request_stop).start()
        summary = recorder.run()
        self.assertEqual(summary["stop_reason"], "requested")
        self.assertGreater(summary["frames_written"], 20)
        self.assertEqual(summary["frames_written"] + summary["frames_dropped_queue"],
                         summary["frames_captured"])
        self.assertEqual(load_manifest(session.root)["status"], "complete")
        report = verify_session(session.root)
        self.assertTrue(report.ok, report.format())

    def test_camera_manifest_marks_uncalibrated_and_records_config(self):
        session, _ = record(self.base, num_frames=2)
        cam = load_manifest(session.root)["camera"]
        self.assertEqual(cam["calibration"], "uncalibrated")
        self.assertEqual(cam["target_fps"], 30)


class VerifyTest(TmpDirCase):
    def make(self, n=50):
        session, _ = record(self.base, num_frames=n)
        return session

    def frame_path(self, session, seq):
        """The real filename for a seq, read from the ledger (it embeds the timestamp)."""
        for line in session.frames_csv.read_text().splitlines()[1:]:
            s_, name, _ = line.split(",")
            if int(s_) == seq:
                return session.frames_dir / Path(name).name
        raise AssertionError(f"seq {seq} not in ledger")

    def test_missing_file_is_an_error(self):
        s = self.make()
        self.frame_path(s, 10).unlink()
        r = verify_session(s.root)
        self.assertFalse(r.ok)
        self.assertTrue(any("missing" in e for e in r.errors))

    def test_orphan_file_in_the_middle_is_an_error(self):
        s = self.make()
        # Remove a ledger row but keep its file.
        lines = s.frames_csv.read_text().splitlines(keepends=True)
        del lines[10]
        s.frames_csv.write_text("".join(lines))
        r = verify_session(s.root)
        self.assertFalse(r.ok)
        self.assertTrue(any("no ledger row" in e for e in r.errors))

    def test_torn_final_row_is_only_a_warning_after_unclean_stop(self):
        s = self.make()
        s.manifest["status"] = "recording"
        s.manifest["stopped"] = None
        s.flush()
        text = s.frames_csv.read_text()
        s.frames_csv.write_text(text[:-10])  # cut mid-row
        r = verify_session(s.root)
        self.assertTrue(r.ok, r.format())
        self.assertTrue(any("torn" in w for w in r.warnings))
        self.assertTrue(any("not closed cleanly" in w for w in r.warnings))

    def test_trailing_unlogged_files_warn_if_unclean_but_error_if_clean(self):
        s = self.make()
        lines = s.frames_csv.read_text().splitlines(keepends=True)
        s.frames_csv.write_text("".join(lines[:-3]))  # last 3 rows lost
        self.assertFalse(verify_session(s.root).ok)  # manifest says complete
        s.manifest["status"] = "recording"
        s.flush()
        r = verify_session(s.root)
        self.assertTrue(r.ok, r.format())
        self.assertTrue(any("after the last ledger row" in w for w in r.warnings))

    def test_zero_length_and_truncated_frames_are_errors(self):
        s = self.make()
        self.frame_path(s, 5).write_bytes(b"")
        self.frame_path(s, 6).write_bytes(synthetic_jpeg(6)[:-1])
        r = verify_session(s.root)
        self.assertEqual(sum("seq 5" in e or "seq 6" in e for e in r.errors), 2)
        self.assertTrue(verify_session(s.root, check_content=False).ok)

    def test_non_monotonic_timestamp_is_an_error(self):
        s = self.make()
        lines = s.frames_csv.read_text().splitlines(keepends=True)
        lines[5], lines[6] = lines[6], lines[5]
        s.frames_csv.write_text("".join(lines))
        r = verify_session(s.root)
        self.assertFalse(r.ok)
        self.assertTrue(any("not increasing" in e for e in r.errors))

    def test_manifest_drop_count_must_match_ledger_gaps(self):
        s = self.make()
        s.manifest["streams"]["frames"]["frames_dropped_queue"] = 7
        s.flush()
        r = verify_session(s.root)
        self.assertFalse(r.ok)
        self.assertTrue(any("dropped" in e for e in r.errors))

    def test_trimmed_to_can_overlap_is_accounted_for_not_flagged_as_a_mismatch(self):
        # A session trim_to_can_overlap has legitimately shrunk: fewer ledger
        # rows (and files) than frames_captured, explained entirely by a
        # recorded trim -- ledger row and file removed together, matching
        # what trim_to_can_overlap.py --delete actually does.
        s = self.make(n=50)
        removed = 10
        lines = s.frames_csv.read_text().splitlines(keepends=True)
        removed_lines, kept_lines = lines[1:1 + removed], lines[1 + removed:]
        for line in removed_lines:
            name = line.split(",")[1]
            (s.root / name).unlink()
        s.frames_csv.write_text("".join([lines[0]] + kept_lines))
        s.manifest["streams"]["frames"]["frames_written"] = 50 - removed
        s.manifest["streams"]["frames"]["trimmed_to_can_overlap"] = {
            "dropped": removed, "kept": 50 - removed,
            "overlap_start_ns": 0, "overlap_end_ns": 0,
        }
        s.flush()
        r = verify_session(s.root)
        self.assertTrue(r.ok, r.format())
        self.assertEqual(r.stats["trimmed_to_can_overlap"]["dropped"], removed)

    def test_trimmed_to_can_overlap_does_not_excuse_a_real_unexplained_mismatch(self):
        # The field being present only excuses exactly its own count -- a
        # further, separate discrepancy must still be caught.
        s = self.make(n=50)
        lines = s.frames_csv.read_text().splitlines(keepends=True)
        s.frames_csv.write_text("".join([lines[0]] + lines[11:]))  # drop 10 rows for real
        s.manifest["streams"]["frames"]["frames_written"] = 40
        s.manifest["streams"]["frames"]["trimmed_to_can_overlap"] = {
            "dropped": 3, "kept": 47,  # claims only 3 were trimmed, not the real 10
            "overlap_start_ns": 0, "overlap_end_ns": 0,
        }
        s.flush()
        r = verify_session(s.root)
        self.assertFalse(r.ok)
        self.assertTrue(any("accounting mismatch" in e for e in r.errors))

    def test_timing_gap_without_seq_gap_is_counted_as_suspected_missing(self):
        # Frames lost upstream of the app leave no seq gap but a timing gap.
        session = Session.create(self.base)

        class Gappy(FakeCameraSource):
            def frames(self):
                for f in super().frames():
                    shift = 2 * round(1e9 / 30) if f.seq >= 20 else 0
                    yield type(f)(f.seq, f.timestamp_ns + shift, f.data)

        recorder = Recorder(session, Gappy(fps=30, num_frames=40, realtime=False, start_ns=0),
                            queue_size=100, flush_interval_s=0.02)
        recorder.run()
        r = verify_session(session.root)
        self.assertTrue(r.ok, r.format())
        self.assertEqual(r.stats["seq_gaps"], 0)
        self.assertEqual(r.stats["suspected_missing_from_timing"], 2)

    def test_drops_pass_but_produce_a_loud_warning(self):
        s = self.make()
        # Drop 5 frames from the middle: ledger rows, files and manifest all agree.
        paths = [self.frame_path(s, n) for n in range(10, 15)]  # look up before editing the ledger
        lines = s.frames_csv.read_text().splitlines(keepends=True)
        del lines[11:16]
        s.frames_csv.write_text("".join(lines))
        for path in paths:
            path.unlink()
        st = s.manifest["streams"]["frames"]
        st["frames_written"] -= 5
        st["frames_dropped_queue"] = 5
        s.flush()
        r = verify_session(s.root)
        self.assertTrue(r.ok, r.format())
        self.assertTrue(any("5 of 50 frames (10.0%) were dropped" in w for w in r.warnings), r.warnings)

    def test_a_multi_second_gap_is_called_out_as_a_stall(self):
        session = Session.create(self.base)

        class Gappy(FakeCameraSource):
            def frames(self):
                for f in super().frames():
                    shift = 4_000_000_000 if f.seq >= 10 else 0
                    yield type(f)(f.seq, f.timestamp_ns + shift, f.data)

        Recorder(session, Gappy(fps=30, num_frames=20, realtime=False, start_ns=0),
                 queue_size=100, flush_interval_s=0.02).run()
        r = verify_session(session.root)
        self.assertTrue(any("longest gap between frames is 4." in w for w in r.warnings), r.warnings)

    def test_clean_capture_has_no_drop_warning(self):
        r = verify_session(self.make().root)
        self.assertFalse(any("dropped" in w or "longest gap" in w for w in r.warnings), r.warnings)

    def test_achieved_fps_and_interval_stats_reported(self):
        s = self.make(100)
        r = verify_session(s.root)
        self.assertAlmostEqual(r.stats["achieved_fps"], 30.0, delta=0.01)
        self.assertAlmostEqual(r.stats["interval_ms"]["mean"], 33.333, delta=0.01)
        self.assertEqual(r.stats["interval_histogram"]["0.9-1.1x"], 99)


if __name__ == "__main__":
    unittest.main()
