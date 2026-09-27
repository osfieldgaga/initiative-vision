import csv
import tempfile
import unittest
from pathlib import Path

from ivision.session import Session, load_manifest
from ivision.trim_to_can_overlap import compute_overlap, trim_session


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


def write_frames_csv(session_root: Path, rows):
    """rows: [(seq, timestamp_ns), ...]. Writes both frames.csv and the
    (empty but present) frame files it references, matching the real
    <seq>_<timestamp_ns>.jpg convention."""
    (session_root / "frames").mkdir(exist_ok=True)
    with open(session_root / "frames.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["seq", "filename", "timestamp_ns"])
        for seq, ts in rows:
            name = f"frames/{seq:08d}_{ts}.jpg"
            writer.writerow([seq, name, ts])
            (session_root / name).write_bytes(b"\xff\xd8fake\xff\xd9")


def write_can_csv(session_root: Path, timestamps):
    with open(session_root / "can.csv", "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["seq", "timestamp_ns", "kernel_realtime_ns", "can_id",
                         "is_extended", "is_remote_frame", "dlc", "data"])
        for i, ts in enumerate(timestamps):
            writer.writerow([i, ts, "", "0x1", "0", "0", 1, "00"])


class ComputeOverlapTest(TmpDirCase):
    def test_overlap_is_the_intersection_of_both_ranges(self):
        session = self.base / "s1"
        session.mkdir()
        write_frames_csv(session, [(0, 0), (1, 1000), (2, 2000), (3, 3000)])
        write_can_csv(session, [500, 1500, 2500])  # can's range: 500-2500
        start, end = compute_overlap(session)
        self.assertEqual((start, end), (500, 2500))


class TrimSessionTest(TmpDirCase):
    def make_session(self, frame_ts, can_ts):
        session = Session.create(self.base)
        write_frames_csv(session.root, [(i, ts) for i, ts in enumerate(frame_ts)])
        write_can_csv(session.root, can_ts)
        return session

    def test_dry_run_reports_but_deletes_nothing(self):
        session = self.make_session(frame_ts=[0, 1000, 2000, 3000], can_ts=[500, 2500])
        summary = trim_session(session.root, delete=False)
        self.assertEqual(summary["total_frames"], 4)
        self.assertEqual(summary["kept"], 2)  # 1000, 2000 fall within [500, 2500]
        self.assertEqual(summary["dropped"], 2)  # 0 and 3000 fall outside
        self.assertFalse(summary["deleted"])
        # Nothing actually removed:
        self.assertEqual(len(list((session.root / "frames").glob("*.jpg"))), 4)
        self.assertEqual(len(session.frames_csv.read_text().splitlines()), 5)  # header + 4

    def test_delete_removes_files_and_rewrites_the_ledger(self):
        session = self.make_session(frame_ts=[0, 1000, 2000, 3000], can_ts=[500, 2500])
        summary = trim_session(session.root, delete=True)
        self.assertEqual(summary["kept"], 2)
        self.assertEqual(summary["dropped"], 2)
        self.assertTrue(summary["deleted"])

        remaining = sorted(p.name for p in (session.root / "frames").glob("*.jpg"))
        self.assertEqual(remaining, ["00000001_1000.jpg", "00000002_2000.jpg"])
        lines = session.frames_csv.read_text().splitlines()
        self.assertEqual(len(lines), 3)  # header + 2 kept rows
        self.assertIn("1000", lines[1])
        self.assertIn("2000", lines[2])

    def test_delete_updates_the_manifest(self):
        session = self.make_session(frame_ts=[0, 1000, 2000, 3000], can_ts=[500, 2500])
        trim_session(session.root, delete=True)
        m = load_manifest(session.root)
        self.assertEqual(m["streams"]["frames"]["frames_written"], 2)
        trimmed = m["streams"]["frames"]["trimmed_to_can_overlap"]
        self.assertEqual(trimmed["dropped"], 2)
        self.assertEqual(trimmed["kept"], 2)

    def test_nothing_to_trim_when_fully_overlapping(self):
        session = self.make_session(frame_ts=[1000, 2000], can_ts=[0, 3000])
        summary = trim_session(session.root, delete=True)
        self.assertEqual(summary["dropped"], 0)
        self.assertFalse(summary["deleted"])  # nothing dropped -> no-op, even with --delete
        self.assertEqual(len(list((session.root / "frames").glob("*.jpg"))), 2)

    def test_missing_frames_csv_raises_clearly(self):
        session = Session.create(self.base)
        write_can_csv(session.root, [0, 1000])
        with self.assertRaises(FileNotFoundError):
            trim_session(session.root)

    def test_missing_can_csv_raises_clearly(self):
        session = Session.create(self.base)
        write_frames_csv(session.root, [(0, 0), (1, 1000)])
        with self.assertRaises(FileNotFoundError):
            trim_session(session.root)

    def test_works_without_a_manifest(self):
        # A hand-built session dir with no session.json at all -- file
        # operations must still succeed; only the manifest update is skipped.
        session_dir = self.base / "no_manifest_session"
        session_dir.mkdir()
        write_frames_csv(session_dir, [(0, 0), (1, 1000), (2, 2000)])
        write_can_csv(session_dir, [500, 1500])
        summary = trim_session(session_dir, delete=True)
        self.assertEqual(summary["dropped"], 2)  # only 1000 falls within [500, 1500]
        self.assertTrue(summary["deleted"])
        self.assertFalse((session_dir / "session.json").exists())

    def test_trimmed_session_still_passes_frames_verify(self):
        from ivision.frames.verify import verify_session

        session = self.make_session(frame_ts=[0, 1000, 2000, 3000], can_ts=[500, 2500])
        # Match the manifest's frame-stream accounting to a clean recording of
        # exactly these 4 frames, the way a real Recorder summary would.
        session.set_stream("frames", frames_captured=4, frames_written=4,
                           frames_dropped_queue=0, frames_dropped_encode=0,
                           frames_unwritten_after_error=0, last_seq_captured=3)
        session.finalize()

        trim_session(session.root, delete=True)

        report = verify_session(session.root, check_content=False)
        self.assertTrue(report.ok, report.format())
        self.assertEqual(report.stats["trimmed_to_can_overlap"]["dropped"], 2)


if __name__ == "__main__":
    unittest.main()
