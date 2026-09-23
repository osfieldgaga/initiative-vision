import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ivision.frames import make_video as mv
from test_frames import record  # real small session, no ffmpeg needed to build it


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class ReadLedgerTest(TmpCase):
    def test_reads_rows_in_order(self):
        session, _ = record(self.base, num_frames=5)
        rows = mv.read_ledger(session.root)
        self.assertEqual(len(rows), 5)
        self.assertEqual([r[0] for r in rows], [0, 1, 2, 3, 4])
        self.assertTrue(rows[0][1].startswith("frames/00000000_"))
        self.assertIsInstance(rows[0][2], int)

    def test_missing_ledger_points_at_rebuild(self):
        with self.assertRaisesRegex(FileNotFoundError, "ivision.frames.rebuild"):
            mv.read_ledger(self.base / "nowhere")

    def test_bad_header_rejected(self):
        d = self.base / "s"
        d.mkdir()
        (d / "frames.csv").write_text("not,the,right,header\n1,2,3,4\n")
        with self.assertRaises(ValueError):
            mv.read_ledger(d)

    def test_header_only_no_rows(self):
        d = self.base / "s"
        d.mkdir()
        (d / "frames.csv").write_text("seq,filename,timestamp_ns\n")
        self.assertEqual(mv.read_ledger(d), [])


class BuildConcatFileTest(unittest.TestCase):
    ROWS = [
        (0, "frames/a.jpg", 1_000_000_000),
        (1, "frames/b.jpg", 1_033_333_333),
        (2, "frames/c.jpg", 1_066_666_666),
    ]

    def test_real_timing_uses_gap_to_next_frame(self):
        text = mv.build_concat_file(self.ROWS)
        lines = text.splitlines()
        self.assertEqual(lines[0], "file 'frames/a.jpg'")
        self.assertAlmostEqual(float(lines[1].split()[1]), 0.033333, places=5)
        self.assertEqual(lines[2], "file 'frames/b.jpg'")
        self.assertAlmostEqual(float(lines[3].split()[1]), 0.033333, places=5)
        self.assertEqual(lines[4], "file 'frames/c.jpg'")
        # last row has no "next" frame: reuses the gap from the frame before it
        self.assertAlmostEqual(float(lines[5].split()[1]), 0.033333, places=5)

    def test_last_frame_is_listed_a_second_time_with_no_duration(self):
        # The documented ffmpeg concat-demuxer workaround: without this, the
        # final frame would not actually hold on screen for its duration.
        lines = mv.build_concat_file(self.ROWS).splitlines()
        self.assertEqual(lines[-1], "file 'frames/c.jpg'")
        self.assertNotIn("duration", lines[-1])
        self.assertEqual(len(lines), 7)  # 3 frames x (file+duration) + one bare repeat

    def test_constant_fps_ignores_real_timing(self):
        text = mv.build_concat_file(self.ROWS, fps=30)
        durations = [float(l.split()[1]) for l in text.splitlines() if l.startswith("duration")]
        self.assertEqual(len(durations), 3)
        for d in durations:
            self.assertAlmostEqual(d, 1 / 30, places=6)

    def test_a_large_gap_from_a_drop_is_preserved_not_smoothed(self):
        rows = [(0, "a.jpg", 0), (1, "b.jpg", 5_000_000_000)]  # a 5s stall
        lines = mv.build_concat_file(rows).splitlines()
        self.assertAlmostEqual(float(lines[1].split()[1]), 5.0, places=3)

    def test_single_frame_falls_back_to_30fps_duration(self):
        lines = mv.build_concat_file([(0, "only.jpg", 123)]).splitlines()
        self.assertAlmostEqual(float(lines[1].split()[1]), 1 / 30, places=6)

    def test_no_frames_raises(self):
        with self.assertRaises(ValueError):
            mv.build_concat_file([])


class RunFfmpegTest(unittest.TestCase):
    def test_missing_ffmpeg_gives_an_install_hint(self):
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "apt install -y ffmpeg"):
                mv.run_ffmpeg("session_dir", "concat.txt", "/tmp/out.mp4")

    def test_command_and_cwd_are_correct(self):
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                mock.patch("subprocess.run") as run:
            mv.run_ffmpeg("/some/session", "_make_video_concat.txt", "/tmp/out.mp4", crf=17)
        args, kwargs = run.call_args
        cmd = args[0]
        self.assertEqual(kwargs["cwd"], "/some/session")
        self.assertTrue(kwargs["check"])
        # the concat file is passed as a bare filename, not session-prefixed,
        # because cwd already points at the session -- prefixing it would look
        # for session/session/_make_video_concat.txt and fail.
        self.assertEqual(cmd[cmd.index("-i") + 1], "_make_video_concat.txt")
        self.assertIn("concat", cmd)
        self.assertIn("-crf", cmd)
        self.assertEqual(cmd[cmd.index("-crf") + 1], "17")
        self.assertEqual(cmd[-1], "/tmp/out.mp4")

    def test_preset_defaults_to_veryfast_not_libx264s_own_default(self):
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                mock.patch("subprocess.run") as run:
            mv.run_ffmpeg("/some/session", "_make_video_concat.txt", "/tmp/out.mp4")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("-preset") + 1], "veryfast")

    def test_preset_is_overridable(self):
        with mock.patch("shutil.which", return_value="/usr/bin/ffmpeg"), \
                mock.patch("subprocess.run") as run:
            mv.run_ffmpeg("/some/session", "_make_video_concat.txt", "/tmp/out.mp4", preset="ultrafast")
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[cmd.index("-preset") + 1], "ultrafast")


class MainTest(TmpCase):
    def test_writes_and_cleans_up_the_concat_file_on_success(self):
        session, _ = record(self.base, num_frames=4)
        with mock.patch.object(mv, "run_ffmpeg") as run_ffmpeg:
            code = mv.main([str(session.root)])
        self.assertEqual(code, 0)
        run_ffmpeg.assert_called_once()
        session_dir, concat_name, out_path = run_ffmpeg.call_args[0][:3]
        self.assertEqual(concat_name, "_make_video_concat.txt")
        self.assertEqual(Path(out_path).name, "preview.mp4")
        self.assertFalse((session.root / "_make_video_concat.txt").exists())  # cleaned up

    def test_concat_file_is_cleaned_up_even_if_ffmpeg_fails(self):
        session, _ = record(self.base, num_frames=4)
        with mock.patch.object(mv, "run_ffmpeg", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                mv.main([str(session.root)])
        self.assertFalse((session.root / "_make_video_concat.txt").exists())

    def test_custom_out_path_is_respected(self):
        session, _ = record(self.base, num_frames=3)
        target = self.base / "clip.mp4"
        with mock.patch.object(mv, "run_ffmpeg") as run_ffmpeg:
            mv.main([str(session.root), "--out", str(target)])
        out_path = run_ffmpeg.call_args[0][2]
        self.assertEqual(Path(out_path), target.resolve())

    def test_zero_or_negative_fps_rejected(self):
        session, _ = record(self.base, num_frames=2)
        with self.assertRaises(SystemExit):
            mv.main([str(session.root), "--fps", "0"])


if __name__ == "__main__":
    unittest.main()
