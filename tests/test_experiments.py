import json
import unittest

from test_camera import FakePicamera2

from ivision.frames import camera_experiments as ex


class ExperimentChildTest(unittest.TestCase):
    def setUp(self):
        FakePicamera2.instances.clear()
        FakePicamera2.fail_capture_at = None
        FakePicamera2.fail_configure = False
        FakePicamera2.on_frame = None
        FakePicamera2.timestamp_fn = None

    def run_child(self, name, seconds=0.3):
        lines = []
        result = ex.run_child(name, seconds, cam_cls=FakePicamera2, out=lines.append, exit_process=False)
        return result, lines

    def test_every_config_runs_and_reports_frames(self):
        for name in ex.CONFIGS:
            with self.subTest(config=name):
                result, lines = self.run_child(name)
                self.assertGreater(result["frames"], 0)
                self.assertIsNone(result["error"])
                self.assertTrue(any(l.startswith("RESULT ") for l in lines))

    def test_config_options_reach_picamera(self):
        self.run_child("F")
        kw = FakePicamera2.instances[-1].config_kwargs
        self.assertEqual(kw["buffer_count"], 12)
        self.run_child("D")
        self.assertNotIn("controls", FakePicamera2.instances[-1].config_kwargs)
        self.run_child("E")
        self.assertIs(FakePicamera2.instances[-1].config_kwargs["queue"], False)
        self.run_child("C")
        self.assertEqual(FakePicamera2.instances[-1].config_kwargs["main"]["format"], "YUV420")

    def test_thread_setting_really_moves_the_capture_loop(self):
        import threading

        seen = []
        FakePicamera2.on_frame = lambda n: seen.append(threading.current_thread() is threading.main_thread())
        self.run_child("A")
        self.assertTrue(seen and all(seen), "A must capture in the main thread")
        seen.clear()
        self.run_child("I")
        self.assertTrue(seen and not any(seen), "I must capture in a worker thread")

    def test_encode_variant_encodes_frames(self):
        result, _ = self.run_child("J")
        self.assertGreater(result["encoded"], 0)

    def test_camera_error_is_reported_not_raised(self):
        FakePicamera2.fail_capture_at = 5
        result, _ = self.run_child("A")
        self.assertIn("camera timeout", result["error"])


class ExperimentParentTest(unittest.TestCase):
    def test_parse_output_finds_result_and_counts_timeouts(self):
        text = ("TICK t=1 frames=30\nERROR Camera frontend has timed out!\n"
                'RESULT {"name": "A", "frames": 6, "fps": 1.0, "error": null}\n')
        p = ex.parse_output(text)
        self.assertEqual(p["result"]["frames"], 6)
        self.assertEqual(p["timeouts"], 1)

    def test_parse_output_of_a_hung_child_keeps_last_tick(self):
        p = ex.parse_output("TICK t=1 frames=0\nTICK t=2 frames=0\n")
        self.assertIsNone(p["result"])
        self.assertEqual(p["last_tick"], "TICK t=2 frames=0")

    def test_verdicts(self):
        good = {"result": {"fps": 29.9, "frames": 180, "error": None}, "timeouts": 0}
        self.assertEqual(ex.verdict(good, False, 6), "OK")
        self.assertEqual(ex.verdict({**good, "timeouts": 1}, False, 6), "SLOW")
        stall = {"result": {"fps": 1.0, "frames": 6, "error": None}, "timeouts": 1}
        self.assertEqual(ex.verdict(stall, False, 6), "STALL")
        self.assertEqual(ex.verdict({"result": None, "timeouts": 0}, True, 6), "HUNG")
        self.assertEqual(ex.verdict({"result": None, "timeouts": 0}, False, 6), "CRASHED")
        err = {"result": {"fps": 0, "frames": 0, "error": "X"}, "timeouts": 0}
        self.assertEqual(ex.verdict(err, False, 6), "ERROR")

    def test_config_a_matches_the_recorders_camera_setup(self):
        # If A drifts from what PicameraSource does, the experiment tells us nothing.
        from ivision.frames.camera import PicameraSource

        a = ex._cfg("A")
        s = PicameraSource()
        self.assertEqual(a["size"], (s.width, s.height))
        self.assertEqual(a["fmt"], "RGB888")
        self.assertEqual(a["buffers"], s.buffer_count)
        self.assertEqual(a["fdl"], True)


if __name__ == "__main__":
    unittest.main()
