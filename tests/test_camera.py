"""PicameraSource and the recorder's encode stage, against a FAKE Picamera2.

The fake follows my reading of the Picamera2 API. These tests check our logic
(config, seq, timestamps, buffer release, shutdown, accounting). They cannot
prove the real Picamera2 behaves the same; only a run on the Pi can.
"""

import io
import tempfile
import threading
import time
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

from ivision.frames.camera import PicameraSource
from ivision.frames.pipeline import Recorder
from ivision.frames.source import FakeCameraSource
from ivision.frames.verify import verify_session
from ivision.session import Session, load_manifest

BASE_TS = 5_000_000_000
INTERVAL = 33_333_333


class FakeRequest:
    def __init__(self, cam, n):
        self.cam, self.n = cam, n

    def get_metadata(self):
        fn = FakePicamera2.timestamp_fn
        ts = fn(self.n) if fn else BASE_TS + self.n * INTERVAL
        return {"SensorTimestamp": ts, "ExposureTime": 20000}

    def make_array(self, name):
        assert name == "main"
        if self.cam.fail_make_array_at == self.n:
            raise RuntimeError("buffer mapping failed")
        h, w = self.cam.frame_hw
        return np.full((h, w, 3), self.n % 256, np.uint8)

    def release(self):
        self.cam.released += 1


class FakePicamera2:
    instances = []
    frame_hw = (48, 64)
    fail_make_array_at = None
    fail_capture_at = None
    fail_configure = False
    on_frame = None  # called with frame number inside capture_request
    timestamp_fn = None  # optional: frame number -> SensorTimestamp

    def __init__(self):
        FakePicamera2.instances.append(self)
        self._modes = [
            {"size": (640, 480), "bit_depth": 10, "fps": 206.65, "crop_limits": (1000, 752, 1280, 960)},
            {"size": (1640, 1232), "bit_depth": 10, "fps": 41.85, "crop_limits": (0, 0, 3280, 2464)},
            {"size": (3280, 2464), "bit_depth": 10, "fps": 21.19, "crop_limits": (0, 0, 3280, 2464)},
        ]
        self.camera_properties = {"Model": "imx219", "PixelArraySize": (3280, 2464)}
        self.n = 0
        self.released = 0
        self.started = self.stopped = self.closed = False
        self.stop_calls = self.close_calls = 0

    @property
    def sensor_modes(self):
        # Real Picamera2 reconfigures the camera to enumerate modes, which is
        # refused once the camera is running (seen on the Pi, 2026-09-21).
        if self.started and not self.stopped:
            raise RuntimeError("Camera must be stopped before configuring")
        return self._modes

    def create_video_configuration(self, **kw):
        self.config_kwargs = kw
        return {"main": kw["main"], "sensor": kw.get("sensor", {}), "controls": kw.get("controls", {}), "raw": None}

    def configure(self, config):
        if FakePicamera2.fail_configure:
            raise RuntimeError("Camera in Configured state trying to configure")
        self.camera_config = config

    def start(self):
        self.started = True

    def capture_metadata(self):
        return {"ScalerCrop": (0, 0, 3280, 2464), "ExposureTime": 20000, "AnalogueGain": 1.5,
                "FrameDuration": 33333, "Unrelated": 1}

    def capture_request(self):
        self.n += 1
        if FakePicamera2.fail_capture_at == self.n:
            raise RuntimeError("camera timeout")
        if FakePicamera2.on_frame:
            FakePicamera2.on_frame(self.n)
        return FakeRequest(self, self.n - 1)

    def stop(self):
        self.stop_calls += 1
        self.stopped = True

    def close(self):
        self.close_calls += 1
        self.closed = True


def make_source(**kw):
    kw.setdefault("picamera2_cls", FakePicamera2)
    return PicameraSource(**kw)


class CameraCase(unittest.TestCase):
    def setUp(self):
        FakePicamera2.instances.clear()
        FakePicamera2.fail_make_array_at = None
        FakePicamera2.fail_capture_at = None
        FakePicamera2.fail_configure = False
        FakePicamera2.on_frame = None
        FakePicamera2.timestamp_fn = None
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    @property
    def cam(self):
        return FakePicamera2.instances[-1]


class SourceTest(CameraCase):
    def test_config_uses_fixed_frame_duration_and_rgb888(self):
        s = make_source(width=1640, height=1232, fps=30)
        s.start()
        kw = self.cam.config_kwargs
        self.assertEqual(kw["controls"]["FrameDurationLimits"], (33333, 33333))
        self.assertEqual(kw["main"], {"size": (1640, 1232), "format": "RGB888"})
        self.assertEqual(kw["sensor"], {})
        self.assertNotIn("AeEnable", kw["controls"])
        self.assertTrue(self.cam.started)

    def test_camera_queue_option_is_passed_through_only_when_set(self):
        s = make_source()
        s.start()
        self.assertNotIn("queue", self.cam.config_kwargs)
        self.assertEqual(s.describe()["camera_queue"], "default")
        s = make_source(queue=False)
        s.start()
        self.assertIs(self.cam.config_kwargs["queue"], False)
        self.assertEqual(s.describe()["camera_queue"], "off")

    def test_sensor_mode_index_selects_that_mode(self):
        s = make_source(sensor_mode=1)
        s.start()
        self.assertEqual(self.cam.config_kwargs["sensor"], {"output_size": (1640, 1232), "bit_depth": 10})

    def test_bad_sensor_mode_raises_and_releases_camera(self):
        s = make_source(sensor_mode=9)
        with self.assertRaises(ValueError) as cm:
            s.start()
        self.assertIn("0..2", str(cm.exception))
        self.assertTrue(self.cam.closed)

    def test_manual_exposure_turns_auto_exposure_off(self):
        s = make_source(exposure_us=8000, analogue_gain=2.0)
        s.start()
        c = self.cam.config_kwargs["controls"]
        self.assertEqual((c["AeEnable"], c["ExposureTime"], c["AnalogueGain"]), (False, 8000, 2.0))

    def test_configure_failure_closes_camera_and_reraises_original_error(self):
        FakePicamera2.fail_configure = True
        s = make_source()
        with self.assertRaisesRegex(RuntimeError, "Configured state"):
            s.start()
        self.assertTrue(self.cam.closed)

    def test_frames_use_sensor_timestamp_and_count_from_zero(self):
        s = make_source()
        s.start()
        it = s.frames()
        frames = [next(it) for _ in range(3)]
        self.assertEqual([f.seq for f in frames], [0, 1, 2])
        self.assertEqual([f.timestamp_ns for f in frames],
                         [BASE_TS, BASE_TS + INTERVAL, BASE_TS + 2 * INTERVAL])
        self.assertEqual(frames[0].data.shape, (48, 64, 3))

    def test_every_request_is_released_promptly(self):
        s = make_source()
        s.start()
        it = s.frames()
        for _ in range(5):
            next(it)
        self.assertEqual(self.cam.released, 5)

    def test_request_is_released_even_if_reading_it_fails(self):
        FakePicamera2.fail_make_array_at = 1
        s = make_source()
        s.start()
        with self.assertRaises(RuntimeError):
            next(s.frames())
            next(s.frames())
        self.assertEqual(self.cam.released, self.cam.n)

    def test_stop_ends_frames_and_close_is_idempotent(self):
        s = make_source()
        s.start()
        FakePicamera2.on_frame = lambda n: s.stop() if n == 4 else None
        self.assertEqual(len(list(s.frames())), 4)
        s.close()
        s.close()
        self.assertEqual((self.cam.stop_calls, self.cam.close_calls), (1, 1))

    def test_describe_records_what_calibration_needs(self):
        s = make_source(width=1640, height=1232, sensor_mode=1, quality=85)
        s.start()
        d = s.describe()
        self.assertEqual(d["kind"], "picamera2")
        self.assertEqual(d["target_fps"], 30.0)
        self.assertEqual(d["sensor_mode_requested"], 1)
        self.assertEqual(d["jpeg_quality"], 85)
        self.assertEqual(d["exposure_mode"], "auto")
        self.assertEqual(d["camera_Model"], "imx219")
        self.assertEqual(d["first_frame_metadata"]["ScalerCrop"], (0, 0, 3280, 2464))
        self.assertNotIn("Unrelated", d["first_frame_metadata"])
        self.assertEqual(d["applied_config"]["sensor"], {"output_size": (1640, 1232), "bit_depth": 10})
        self.assertEqual(len(d["sensor_modes_available"]), 3)

    def test_sensor_modes_and_describe_work_after_the_camera_is_running(self):
        # Regression: reading cam.sensor_modes on a started camera raised
        # "Camera must be stopped before configuring" on the real Pi.
        s = make_source()
        s.start()
        with self.assertRaisesRegex(RuntimeError, "must be stopped"):
            self.cam.sensor_modes  # the fake now behaves like the real property
        self.assertEqual(len(s.sensor_modes()), 3)
        self.assertEqual(len(s.describe()["sensor_modes_available"]), 3)

    def test_source_records_and_uses_the_chosen_encoder(self):
        s = make_source(encoder="pillow")
        s.start()
        self.assertEqual(s.describe()["jpeg_encoder"], "pillow")
        with Image.open(io.BytesIO(s.encode(next(s.frames())))) as img:
            self.assertEqual(img.size, (64, 48))
        self.assertIn(make_source().describe()["jpeg_encoder"], ("simplejpeg", "pillow"))

    def test_rotate_deg_rejects_a_value_thats_not_a_right_angle(self):
        with self.assertRaises(ValueError):
            make_source(rotate_deg=45)

    def test_encode_applies_the_rotation_before_compressing(self):
        # The fake camera always hands back a fixed (h=48, w=64, 3) frame regardless of
        # the requested width/height, so a 90-degree rotation swapping the JPEG's actual
        # dimensions to (48, 64) is unambiguous proof encode() really rotated the pixels,
        # not just relabelled something in the manifest.
        s = make_source(rotate_deg=90, encoder="pillow")
        s.start()
        with Image.open(io.BytesIO(s.encode(next(s.frames())))) as img:
            self.assertEqual(img.size, (48, 64))  # swapped from the unrotated (64, 48)

        unrotated = make_source(rotate_deg=0, encoder="pillow")
        unrotated.start()
        with Image.open(io.BytesIO(unrotated.encode(next(unrotated.frames())))) as img:
            self.assertEqual(img.size, (64, 48))

    def test_describe_reports_saved_size_accounting_for_rotation(self):
        s = make_source(width=1280, height=960, rotate_deg=0)
        self.assertEqual(s.describe()["saved_size"], [1280, 960])
        s = make_source(width=1280, height=960, rotate_deg=180)
        self.assertEqual(s.describe()["saved_size"], [1280, 960])  # unchanged
        s = make_source(width=1280, height=960, rotate_deg=90)
        self.assertEqual(s.describe()["saved_size"], [960, 1280])  # swapped
        s = make_source(width=1280, height=960, rotate_deg=270)
        self.assertEqual(s.describe()["saved_size"], [960, 1280])
        # A square size is unaffected either way, which is exactly why it was chosen
        # for the real device (2026-09-22): no width/height bookkeeping needed.
        s = make_source(width=1280, height=1280, rotate_deg=90)
        self.assertEqual(s.describe()["saved_size"], [1280, 1280])

    def test_describe_before_start_is_safe(self):
        d = make_source().describe()
        self.assertEqual(d["kind"], "picamera2")
        self.assertEqual(d["first_frame_metadata"], {})

    def test_encode_gives_decodable_jpeg_of_frame_size(self):
        s = make_source(quality=80)
        s.start()
        frame = next(s.frames())
        with Image.open(io.BytesIO(s.encode(frame))) as img:
            self.assertEqual(img.size, (64, 48))


def record_camera(base, frames, encode_workers=0, source=None, **kw):
    source = source or make_source()
    session = Session.create(base)
    recorder = Recorder(session, source, encode_workers=encode_workers,
                        flush_interval_s=0.02, stop_timeout_s=1.0, **kw)
    FakePicamera2.on_frame = lambda n: source.stop() if n >= frames else None
    return session, recorder.run(), source


class RecorderWithCameraTest(CameraCase):
    def _check_session(self, session, expected_frames):
        report = verify_session(session.root)
        self.assertTrue(report.ok, report.format())
        self.assertEqual(report.stats["rows"], expected_frames)
        for p in sorted(session.frames_dir.glob("*.jpg"))[:5]:
            with Image.open(p) as img:
                self.assertEqual(img.size, (64, 48))

    def test_inline_encode(self):
        session, summary, _ = record_camera(self.base, 60, encode_workers=0)
        self.assertEqual(summary["stop_reason"], "source_ended")
        self.assertEqual(summary["frames_written"], 60)
        self._check_session(session, 60)

    def test_worker_pool_encode_keeps_ledger_in_capture_order(self):
        class SlowEvenFrames(PicameraSource):
            def encode(self, frame):
                time.sleep(0.02 if frame.seq % 2 == 0 else 0.0)  # later frames finish first
                return super().encode(frame)

        source = SlowEvenFrames(picamera2_cls=FakePicamera2)
        session, summary, _ = record_camera(self.base, 40, encode_workers=4, source=source,
                                            max_inflight=40, queue_size=100)
        self.assertEqual(summary["frames_written"], 40)
        self._check_session(session, 40)
        seqs = [int(l.split(",")[0]) for l in session.frames_csv.read_text().splitlines()[1:]]
        self.assertEqual(seqs, list(range(40)))

    def test_saturated_encoder_drops_are_counted_separately_and_reconcile(self):
        class SlowEncoder(PicameraSource):
            def encode(self, frame):
                time.sleep(0.01)
                return super().encode(frame)

        source = SlowEncoder(picamera2_cls=FakePicamera2)
        session, summary, _ = record_camera(self.base, 200, encode_workers=1, source=source,
                                            max_inflight=2)
        self.assertGreater(summary["frames_dropped_encode"], 0)
        self.assertEqual(summary["frames_dropped_queue"], 0)
        self.assertEqual(
            summary["frames_written"] + summary["frames_dropped_encode"], summary["frames_captured"]
        )
        report = verify_session(session.root)
        self.assertTrue(report.ok, report.format())
        self.assertEqual(load_manifest(session.root)["streams"]["frames"]["encode_workers"], 1)

    def test_encoder_threads_run_at_lower_priority_than_capture(self):
        import os
        import threading as th

        seen = {}

        class Probe(PicameraSource):
            def encode(self, frame):
                seen["encoder"] = os.getpriority(os.PRIO_PROCESS, th.get_native_id())
                return super().encode(frame)

        source = Probe(picamera2_cls=FakePicamera2)
        record_camera(self.base, 20, encode_workers=2, source=source, max_inflight=40)
        self.assertEqual(seen["encoder"], 10)
        self.assertEqual(os.getpriority(os.PRIO_PROCESS, th.get_native_id()), 0)  # this thread unchanged

    def test_encode_nice_can_be_switched_off(self):
        import os
        import threading as th

        seen = {}

        class Probe(PicameraSource):
            def encode(self, frame):
                seen["encoder"] = os.getpriority(os.PRIO_PROCESS, th.get_native_id())
                return super().encode(frame)

        source = Probe(picamera2_cls=FakePicamera2)
        session = Session.create(self.base)
        recorder = Recorder(session, source, encode_workers=1, encode_nice=None, flush_interval_s=0.02)
        FakePicamera2.on_frame = lambda n: source.stop() if n >= 5 else None
        recorder.run()
        self.assertEqual(seen["encoder"], 0)

    def test_manifest_has_camera_config_and_stays_uncalibrated(self):
        session, _, _ = record_camera(self.base, 5, source=make_source(sensor_mode=1))
        cam = load_manifest(session.root)["camera"]
        self.assertEqual(cam["calibration"], "uncalibrated")
        self.assertEqual(cam["kind"], "picamera2")
        self.assertEqual(cam["sensor_mode_requested"], 1)
        # Filled in after the camera started, not at construction time:
        self.assertEqual(cam["first_frame_metadata"]["ExposureTime"], 20000)

    def test_camera_failing_to_start_is_reported_and_session_still_closes(self):
        FakePicamera2.fail_configure = True
        session = Session.create(self.base)
        recorder = Recorder(session, make_source(), flush_interval_s=0.02)
        summary = recorder.run()
        self.assertTrue(summary["stop_reason"].startswith("source_error: start failed"), summary)
        self.assertEqual(summary["frames_written"], 0)
        self.assertEqual(load_manifest(session.root)["status"], "complete")

    def test_camera_dying_mid_run_keeps_frames_already_captured(self):
        FakePicamera2.fail_capture_at = 21
        source = make_source()
        session = Session.create(self.base)
        # max_inflight is large so the instant fake camera can't saturate the encoders.
        recorder = Recorder(session, source, encode_workers=2, max_inflight=64,
                            flush_interval_s=0.02)
        summary = recorder.run()
        self.assertTrue(summary["stop_reason"].startswith("source_error"), summary)
        self.assertIn("camera timeout", summary["stop_reason"])
        self.assertEqual(summary["frames_written"], 20)
        self.assertTrue(verify_session(session.root).ok)

    def test_camera_released_after_run(self):
        _, _, source = record_camera(self.base, 10)
        self.assertTrue(self.cam.closed)

    def test_hung_capture_does_not_hang_shutdown(self):
        class Hangs(FakeCameraSource):
            def frames(self):
                yield from super().frames()
                threading.Event().wait()  # never returns: a stuck camera driver

        source = Hangs(fps=30, num_frames=5, realtime=False, start_ns=0)
        session = Session.create(self.base)
        recorder = Recorder(session, source, flush_interval_s=0.02, stop_timeout_s=0.3)
        threading.Timer(0.2, recorder.request_stop).start()
        t0 = time.monotonic()
        summary = recorder.run()
        self.assertLess(time.monotonic() - t0, 3)
        self.assertIn("did not stop", summary["stop_reason"])
        self.assertEqual(summary["frames_written"], 5)

    def test_close_is_still_attempted_when_the_capture_thread_never_stops(self):
        # Regression (2026-09-22): a capture thread stuck forever inside the source (as a
        # real libcamera call can be) used to skip close() entirely, leaking the camera's
        # DMA buffers (CMA) until the Pi was rebooted. close() must be attempted regardless.
        closed = threading.Event()

        class Hangs(FakeCameraSource):
            def frames(self):
                yield from super().frames()
                threading.Event().wait()  # never returns: a stuck camera driver

            def close(self):
                closed.set()

        source = Hangs(fps=30, num_frames=5, realtime=False, start_ns=0)
        session = Session.create(self.base)
        recorder = Recorder(session, source, flush_interval_s=0.02, stop_timeout_s=0.3)
        threading.Timer(0.2, recorder.request_stop).start()
        recorder.run()
        self.assertTrue(closed.is_set())

    def test_camera_that_stops_delivering_ends_the_run_with_a_clear_reason(self):
        class Stalls(FakeCameraSource):
            def frames(self):
                yield from super().frames()
                threading.Event().wait()  # camera stops delivering, no error raised

        source = Stalls(fps=30, num_frames=6, realtime=False, start_ns=0)
        session = Session.create(self.base)
        recorder = Recorder(session, source, flush_interval_s=0.02,
                            stall_timeout_s=0.3, stop_timeout_s=0.3)
        t0 = time.monotonic()
        summary = recorder.run()  # nobody calls request_stop
        self.assertLess(time.monotonic() - t0, 3)
        self.assertIn("no frame for 0.3s", summary["stop_reason"])
        self.assertIn("6 frames received", summary["stop_reason"])
        self.assertEqual(summary["frames_written"], 6)
        self.assertTrue(verify_session(session.root).ok)

    def test_camera_that_never_delivers_a_first_frame_is_a_stall_too(self):
        class Silent(FakeCameraSource):
            def frames(self):
                threading.Event().wait()
                yield  # pragma: no cover

        session = Session.create(self.base)
        recorder = Recorder(session, Silent(), flush_interval_s=0.02,
                            stall_timeout_s=0.3, stop_timeout_s=0.2)
        summary = recorder.run()
        self.assertIn("no frame for", summary["stop_reason"])
        self.assertIn("0 frames received", summary["stop_reason"])

    def test_stall_watchdog_can_be_turned_off(self):
        source = FakeCameraSource(fps=5, num_frames=3, realtime=True)  # 200 ms between frames
        session = Session.create(self.base)
        summary = Recorder(session, source, flush_interval_s=0.02, stall_timeout_s=None).run()
        self.assertEqual(summary["stop_reason"], "source_ended")

    def test_slow_camera_start_is_measured(self):
        class SlowStart(FakeCameraSource):
            def start(self):
                time.sleep(0.25)
                super().start()

        source = SlowStart(fps=30, num_frames=3, realtime=False, start_ns=0)
        session = Session.create(self.base)
        summary = Recorder(session, source, flush_interval_s=0.02).run()
        self.assertGreaterEqual(summary["source_start_s"], 0.25)
        self.assertEqual(load_manifest(session.root)["streams"]["frames"]["source_start_s"],
                         summary["source_start_s"])

    def test_close_failure_is_reported(self):
        class BadClose(FakeCameraSource):
            def close(self):
                raise RuntimeError("device busy")

        source = BadClose(fps=30, num_frames=5, realtime=False, start_ns=0)
        session = Session.create(self.base)
        summary = Recorder(session, source, flush_interval_s=0.02).run()
        self.assertIn("close failed", summary["stop_reason"])
        self.assertEqual(summary["frames_written"], 5)


class ProbeTest(CameraCase):
    def run_probe(self, argv):
        import contextlib
        from functools import partial
        from unittest import mock

        from ivision.frames import camera_probe

        out = io.StringIO()
        with mock.patch.object(camera_probe, "PicameraSource",
                               partial(PicameraSource, picamera2_cls=FakePicamera2)), \
                contextlib.redirect_stdout(out):
            code = camera_probe.main(argv)
        return code, out.getvalue()

    def test_reports_modes_rate_encode_time_and_saves_samples(self):
        # Timestamps that sit just behind "now" on the monotonic clock, 33.3 ms apart.
        t0 = time.monotonic_ns()
        FakePicamera2.timestamp_fn = lambda n: t0 - 30_000_000 + n * INTERVAL
        code, out = self.run_probe(["--frames", "30", "--save-samples", str(self.base / "samples")])
        self.assertEqual(code, 0, out)
        self.assertIn("[1] size=(1640, 1232)", out)
        self.assertIn("Achieved fps", out)
        self.assertIn("30.00", out)
        self.assertIn("JPEG encode", out)
        self.assertIn("on this machine's monotonic timeline", out)
        self.assertEqual(len(list((self.base / "samples").glob("*.jpg"))), 3)

    def test_warns_when_sensor_timestamps_are_not_on_our_clock(self):
        FakePicamera2.timestamp_fn = lambda n: 1_000_000 + n * INTERVAL  # ~1 ms after boot
        code, out = self.run_probe(["--frames", "10"])
        self.assertEqual(code, 0)
        self.assertIn("WARNING: SensorTimestamp is NOT close", out)

    def test_camera_that_cannot_start_gives_a_useful_message(self):
        FakePicamera2.fail_configure = True
        code, out = self.run_probe(["--frames", "5"])
        self.assertEqual(code, 2)
        self.assertIn("could not start the camera", out)
        self.assertIn("rpicam-hello --list-cameras", out)


if __name__ == "__main__":
    unittest.main()
