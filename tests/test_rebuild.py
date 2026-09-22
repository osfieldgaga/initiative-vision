import tempfile
import unittest
from pathlib import Path

from ivision.frames import rebuild as rb
from ivision.frames.pipeline import write_frame_file
from ivision.frames.source import synthetic_jpeg
from ivision.frames.verify import verify_session
from ivision.session import Session
from test_frames import record  # the small helper that runs a real recording


def name(seq, ts, ext="jpg"):
    return f"{seq:08d}_{ts}.{ext}"


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class ScanTest(TmpCase):
    def make_frame(self, session, seq, ts, data=None):
        write_frame_file(session.frames_dir / name(seq, ts), data or synthetic_jpeg(seq))

    def test_scans_and_sorts_by_seq_regardless_of_directory_order(self):
        s = Session.create(self.base)
        self.make_frame(s, 2, 3000)
        self.make_frame(s, 0, 1000)
        self.make_frame(s, 1, 2000)
        rows, bad = rb.scan_frames(s.frames_dir)
        self.assertEqual(rows, [(0, "frames/00000000_1000.jpg", 1000),
                                (1, "frames/00000001_2000.jpg", 2000),
                                (2, "frames/00000002_3000.jpg", 3000)])
        self.assertEqual(bad, [])

    def test_tmp_files_are_ignored_not_reported_as_bad(self):
        s = Session.create(self.base)
        self.make_frame(s, 0, 1000)
        (s.frames_dir / "00000001_2000.jpg.tmp").write_bytes(b"partial")
        rows, bad = rb.scan_frames(s.frames_dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(bad, [])

    def test_unparseable_names_are_reported_and_skipped(self):
        s = Session.create(self.base)
        self.make_frame(s, 0, 1000)
        (s.frames_dir / "not_a_frame.jpg").write_bytes(b"?")
        (s.frames_dir / "00000001.jpg").write_bytes(b"?")  # old pre-2026-09-22 naming
        rows, bad = rb.scan_frames(s.frames_dir)
        self.assertEqual(len(rows), 1)
        self.assertEqual(sorted(bad), ["00000001.jpg", "not_a_frame.jpg"])

    def test_missing_directory_gives_empty_results_not_an_error(self):
        rows, bad = rb.scan_frames(self.base / "nowhere")
        self.assertEqual((rows, bad), ([], []))


class WriteLedgerTest(TmpCase):
    def test_writes_header_and_rows_and_is_atomic(self):
        path = self.base / "out.csv"
        rb.write_ledger(path, [(0, "frames/a.jpg", 100), (1, "frames/b.jpg", 200)])
        self.assertEqual(path.read_text(), "seq,filename,timestamp_ns\n0,frames/a.jpg,100\n1,frames/b.jpg,200\n")
        self.assertFalse(path.with_name("out.csv.tmp").exists())

    def test_overwrites_its_own_output_cleanly(self):
        path = self.base / "out.csv"
        rb.write_ledger(path, [(0, "frames/a.jpg", 100)])
        rb.write_ledger(path, [(0, "frames/a.jpg", 100), (1, "frames/b.jpg", 200)])
        self.assertEqual(len(path.read_text().splitlines()), 3)


class MainTest(TmpCase):
    def make_session(self, n=20):
        session, _ = record(self.base, num_frames=n)
        return session

    def test_report_only_writes_nothing(self):
        s = self.make_session()
        code = rb.main([str(s.root)])
        self.assertEqual(code, 0)
        self.assertFalse((s.root / "frames.rebuilt.csv").exists())

    def test_write_creates_a_correct_ledger_without_touching_the_original(self):
        s = self.make_session()
        original = s.frames_csv.read_text()
        code = rb.main([str(s.root), "--write"])
        self.assertEqual(code, 0)
        self.assertEqual(s.frames_csv.read_text(), original)
        rebuilt = s.root / "frames.rebuilt.csv"
        self.assertEqual(rebuilt.read_text(), original)  # a clean session rebuilds byte-identical

    def test_replace_backs_up_the_old_ledger_and_swaps_in_the_rebuilt_one(self):
        s = self.make_session()
        # Simulate ledger loss: truncate it, as a power cut mid-flush might.
        original = s.frames_csv.read_text()
        s.frames_csv.write_text("seq,filename,timestamp_ns\n0,")
        code = rb.main([str(s.root), "--replace"])
        self.assertEqual(code, 0)
        self.assertEqual(s.frames_csv.read_text(), original)  # fully recovered from the files
        self.assertEqual((s.root / "frames.csv.orig").read_text(), "seq,filename,timestamp_ns\n0,")
        self.assertFalse((s.root / "frames.rebuilt.csv").exists())  # consumed into frames.csv
        report = verify_session(s.root)
        self.assertTrue(report.ok, report.format())

    def test_replace_refuses_to_clobber_an_existing_backup(self):
        s = self.make_session()
        (s.root / "frames.csv.orig").write_text("precious\n")
        code = rb.main([str(s.root), "--replace"])
        self.assertEqual(code, 1)
        self.assertEqual((s.root / "frames.csv.orig").read_text(), "precious\n")
        self.assertTrue(s.frames_csv.exists())  # left alone

    def test_no_frames_at_all_is_reported_and_fails(self):
        (self.base / "empty").mkdir()
        code = rb.main([str(self.base / "empty")])
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
