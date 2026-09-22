import unittest

from test_camera import FakePicamera2

from ivision.frames import encode_bench as eb


class BenchTest(unittest.TestCase):
    def test_synthetic_frame_shape(self):
        self.assertEqual(eb.synthetic_frame(64, 48).shape, (48, 64, 3))

    def test_run_bench_times_available_encoders_and_marks_missing_ones(self):
        arr = eb.synthetic_frame(128, 96)
        rows = eb.run_bench(arr, quality=90, seconds=0.1, thread_counts=(2,))
        by = {r["name"]: r for r in rows}
        self.assertTrue(by["pillow_bgr"]["available"])
        self.assertGreater(by["pillow_bgr"]["fps"], 0)
        self.assertGreater(by["pillow_bgr"]["threads"][2], 0)
        self.assertGreater(by["pillow_bgr"]["kb"], 0)
        for r in rows:  # anything not installed here must be reported, not crash the run
            if not r["available"]:
                self.assertIn("No module named", r["why"])

    def test_missing_encoder_library_is_reported_as_unavailable(self):
        def needs_missing_lib(arr, q):
            import a_library_that_does_not_exist  # noqa: F401

        rows = eb.run_bench(eb.synthetic_frame(32, 32), seconds=0.05, thread_counts=(2,),
                            encoders={"ghost": needs_missing_lib})
        self.assertFalse(rows[0]["available"])

    def test_non_jpeg_output_is_caught(self):
        with self.assertRaises(AssertionError):
            eb.time_single(lambda a, q: b"not a jpeg", eb.synthetic_frame(32, 32), 90, 0.05)

    def test_capture_real_frame_uses_a_copy_and_releases_the_camera(self):
        FakePicamera2.instances.clear()
        FakePicamera2.fail_configure = False
        FakePicamera2.fail_capture_at = None
        FakePicamera2.on_frame = None
        arr = eb.capture_real_frame(64, 48, cam_cls=FakePicamera2)
        self.assertEqual(arr.shape, (48, 64, 3))
        cam = FakePicamera2.instances[-1]
        self.assertTrue(cam.closed)
        self.assertEqual(cam.released, cam.n)

    def test_report_prints_without_error(self):
        import contextlib
        import io

        arr = eb.synthetic_frame(64, 48)
        rows = eb.run_bench(arr, seconds=0.05, thread_counts=(2, 4))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            eb.print_report(arr, 90, rows, 0.05)
        self.assertIn("pillow_bgr", out.getvalue())


if __name__ == "__main__":
    unittest.main()
