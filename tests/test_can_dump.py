import io
import unittest
from contextlib import redirect_stdout

from ivision.can.dump import format_frame, parse_filter, run
from ivision.can.source import CanFrame, FakeCanSource


class Args:
    def __init__(self, **kw):
        self.count = None
        self.duration = None
        self.quiet = False
        self.top = 10
        self.__dict__.update(kw)


class ParseFilterTest(unittest.TestCase):
    def test_parses_hex_id_and_mask(self):
        self.assertEqual(parse_filter("1F1:7FF"), (0x1F1, 0x7FF))

    def test_accepts_0x_prefix(self):
        self.assertEqual(parse_filter("0x1F1:0x7FF"), (0x1F1, 0x7FF))

    def test_missing_colon_is_an_error(self):
        with self.assertRaises(Exception):
            parse_filter("1F1")


class FormatFrameTest(unittest.TestCase):
    def test_standard_id_formatting(self):
        frame = CanFrame(0, None, 0x1F1, False, False, 4, b"\xde\xad\xbe\xef")
        line = format_frame(frame, 1.5)
        self.assertIn("1F1", line)
        self.assertIn("DE AD BE EF", line)
        self.assertNotIn("x", line.split("DLC")[0])  # no extended marker

    def test_extended_id_gets_x_suffix_and_wider_field(self):
        frame = CanFrame(0, None, 0x1ABCDE, True, False, 2, b"\x01\x02")
        line = format_frame(frame, 0.0)
        self.assertIn("001ABCDEx", line)

    def test_remote_frame_gets_r_marker(self):
        frame = CanFrame(0, None, 0x42, False, True, 0, b"")
        self.assertIn(" R ", format_frame(frame, 0.0))


class RunTest(unittest.TestCase):
    def run_dump(self, source, args, stop_after_calls=None):
        buf = io.StringIO()
        calls = {"n": 0}

        def stop_requested():
            calls["n"] += 1
            return stop_after_calls is not None and calls["n"] > stop_after_calls

        with redirect_stdout(buf):
            code = run(source, args, stop_requested=stop_requested)
        return code, buf.getvalue()

    def test_stops_after_count_and_reports_a_summary(self):
        source = FakeCanSource(hz=1000, realtime=False, start_ns=0,
                               messages=[(0x0C9, b"\x01"), (0x1F1, b"\x02")])
        code, out = self.run_dump(source, Args(count=5))
        self.assertEqual(code, 0)
        self.assertIn("5 frames in", out)
        self.assertIn("2 distinct arbitration IDs", out)
        self.assertIn("0C9", out)
        self.assertIn("1F1", out)

    def test_quiet_mode_suppresses_per_frame_lines(self):
        source = FakeCanSource(hz=1000, realtime=False, start_ns=0)
        code, out = self.run_dump(source, Args(count=5, quiet=True))
        self.assertNotIn("DLC=", out)
        self.assertIn("5 frames in", out)

    def test_no_frames_returns_nonzero(self):
        source = FakeCanSource(hz=1000, num_frames=0, realtime=False, start_ns=0)
        code, out = self.run_dump(source, Args())
        self.assertEqual(code, 1)
        self.assertIn("0 frames in", out)

    def test_stop_requested_ends_the_loop_promptly(self):
        source = FakeCanSource(hz=1000, realtime=False, start_ns=0)  # endless (no num_frames)
        code, out = self.run_dump(source, Args(), stop_after_calls=7)
        self.assertIn("7 frames in", out)


if __name__ == "__main__":
    unittest.main()
