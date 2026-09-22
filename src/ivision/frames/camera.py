"""IMX219 frame source on Picamera2 (libcamera).

Proven on real hardware (2026-09-21/22): see docs/capture-performance.md for
the settings that ran cleanly. The Picamera2 calls are all in this file so
any mismatch with the real library is easy to find.

How it works:

* The sensor free-runs at a fixed frame duration (``FrameDurationLimits``), so
  the frame cadence is set by the sensor, not by a software timer.
* Each frame's timestamp is libcamera's ``SensorTimestamp`` (nanoseconds). Which
  kernel clock that is gets checked by ``camera_probe``; it should sit on the
  same timeline as ``time.monotonic_ns()``.
* ``frames()`` yields raw pixel arrays. ``encode(frame)`` turns one into JPEG
  bytes and is run by the recorder inline or in a worker pool.

Picamera2 is imported only in ``start()`` so this module imports on a machine
without it.
"""

from __future__ import annotations

import threading
from typing import Callable, Iterator

from .encode import VALID_ROTATIONS, pick_encoder, rotate_cw
from .source import Frame

# Metadata worth keeping in the manifest: it defines what the image geometry
# and exposure were, which matters for a later calibration.
_FIRST_FRAME_KEYS = (
    "ScalerCrop",
    "ExposureTime",
    "AnalogueGain",
    "DigitalGain",
    "FrameDuration",
    "ColourGains",
    "LensPosition",
)


class PicameraSource:
    def __init__(
        self,
        width: int = 1640,
        height: int = 1232,
        fps: float = 30.0,
        quality: int = 90,
        sensor_mode: int | None = None,
        exposure_us: int | None = None,
        analogue_gain: float | None = None,
        buffer_count: int = 6,
        picamera2_cls: Callable | None = None,
        encoder: str = "auto",
        queue: bool | None = None,
        rotate_deg: int = 0,
    ):
        self.width = width
        self.height = height
        self.fps = fps
        self.quality = quality
        self.sensor_mode = sensor_mode
        self.exposure_us = exposure_us
        self.analogue_gain = analogue_gain
        self.buffer_count = buffer_count
        if rotate_deg not in VALID_ROTATIONS:
            raise ValueError(f"rotate_deg must be one of {VALID_ROTATIONS}, got {rotate_deg}")
        # The Pi's ISP can't rotate in hardware (see encode.py's rotate_cw), so this is
        # applied in software, in encode(), before the frame is compressed.
        self.rotate_deg = rotate_deg
        # None = Picamera2's default (completed requests queue up until the app takes
        # them). False = Picamera2 hands over only the latest frame and recycles older
        # ones itself, so a slow consumer can never starve the camera of buffers.
        self.queue = queue
        self._picamera2_cls = picamera2_cls
        self.encoder_name, self._encode_fn = pick_encoder(encoder)
        self._cam = None
        self._config: dict | None = None
        self._modes: list = []
        self._first_metadata: dict = {}
        self._stop = threading.Event()
        self._closed = False

    # -- configuration -----------------------------------------------------

    @property
    def frame_duration_us(self) -> int:
        return int(round(1_000_000 / self.fps))

    def _controls(self) -> dict:
        d = self.frame_duration_us
        controls = {"FrameDurationLimits": (d, d)}
        if self.exposure_us is not None or self.analogue_gain is not None:
            controls["AeEnable"] = False
            if self.exposure_us is not None:
                controls["ExposureTime"] = int(self.exposure_us)
            if self.analogue_gain is not None:
                controls["AnalogueGain"] = float(self.analogue_gain)
        return controls

    def sensor_modes(self) -> list:
        """The sensor's modes as Picamera2 reported them at ``start()``.

        Cached because Picamera2's ``sensor_modes`` briefly reconfigures the
        camera to find out, which raises "Camera must be stopped before
        configuring" once the camera is running.
        """
        return list(self._modes)

    # -- FrameSource interface ---------------------------------------------

    def start(self) -> None:
        if self._picamera2_cls is None:
            from picamera2 import Picamera2  # imported late: only exists on the Pi

            self._picamera2_cls = Picamera2
        cam = self._picamera2_cls()
        self._cam = cam
        self._closed = False
        try:
            # Must happen before configure()/start(); see sensor_modes().
            self._modes = list(cam.sensor_modes)
            sensor = {}
            if self.sensor_mode is not None:
                modes = self._modes
                if not 0 <= self.sensor_mode < len(modes):
                    raise ValueError(
                        f"sensor mode {self.sensor_mode} does not exist; "
                        f"this camera reports {len(modes)} modes (0..{len(modes) - 1})"
                    )
                mode = modes[self.sensor_mode]
                sensor = {"output_size": mode["size"], "bit_depth": mode["bit_depth"]}
            extra = {} if self.queue is None else {"queue": self.queue}
            config = cam.create_video_configuration(
                main={"size": (self.width, self.height), "format": "RGB888"},
                sensor=sensor,
                controls=self._controls(),
                buffer_count=self.buffer_count,
                **extra,
            )
            cam.configure(config)
            cam.start()
            # The first frame's metadata tells us the crop and exposure actually
            # in effect. That frame is consumed here and not recorded.
            metadata = cam.capture_metadata()
            self._first_metadata = {k: metadata[k] for k in _FIRST_FRAME_KEYS if k in metadata}
            self._config = getattr(cam, "camera_config", None)
        except Exception:
            try:
                self.close()
            except Exception:
                pass  # keep the original error, not a cleanup error
            raise
        self._stop.clear()

    def stop(self) -> None:
        self._stop.set()

    def frames(self) -> Iterator[Frame]:
        cam = self._cam
        seq = 0
        while not self._stop.is_set():
            request = cam.capture_request()
            try:
                timestamp_ns = int(request.get_metadata()["SensorTimestamp"])
                # make_array returns a copy of the buffer, so the array stays
                # valid after release(). If saved frames ever look torn or show
                # the wrong frame, this is the line to suspect.
                pixels = request.make_array("main")
            finally:
                request.release()  # hand the buffer back to libcamera at once
            yield Frame(seq, timestamp_ns, pixels, "jpg")
            seq += 1

    def encode(self, frame: Frame) -> bytes:
        data = rotate_cw(frame.data, self.rotate_deg) if self.rotate_deg else frame.data
        return self._encode_fn(data, self.quality)

    def close(self) -> None:
        cam, self._cam = self._cam, None
        if cam is None or self._closed:
            return
        self._closed = True
        try:
            cam.stop()
        finally:
            cam.close()

    def describe(self) -> dict:
        info = {
            "kind": "picamera2",
            "target_fps": self.fps,
            "frame_duration_us": self.frame_duration_us,
            "main_size_requested": [self.width, self.height],
            "main_format": "RGB888",
            "jpeg_quality": self.quality,
            "jpeg_encoder": self.encoder_name,
            "rotate_deg": self.rotate_deg,
            # The saved JPEG's actual dimensions: unchanged by a 0/180 rotation, swapped
            # by 90/270 on anything non-square. Distinct from main_size_requested (the
            # capture size before rotation) so a reader never has to work this out itself.
            "saved_size": [self.height, self.width] if self.rotate_deg in (90, 270)
                          else [self.width, self.height],
            "camera_queue": "default" if self.queue is None else ("on" if self.queue else "off"),
            "sensor_mode_requested": self.sensor_mode,
            "exposure_mode": "manual" if self._manual_exposure() else "auto",
            "exposure_time_us_requested": self.exposure_us,
            "analogue_gain_requested": self.analogue_gain,
            "first_frame_metadata": self._first_metadata,
        }
        try:
            from importlib.metadata import version

            info["picamera2_version"] = version("picamera2")
        except Exception:
            pass
        cam = self._cam
        if cam is not None:
            props = getattr(cam, "camera_properties", None) or {}
            for key in ("Model", "PixelArraySize", "ScalerCropMaximum"):
                if key in props:
                    info[f"camera_{key}"] = props[key]
            info["sensor_modes_available"] = [
                {k: m.get(k) for k in ("size", "bit_depth", "fps", "crop_limits")}
                for m in self._modes
            ]
        if self._config is not None:
            # The configuration Picamera2 actually applied. Calibration is only
            # valid for this exact geometry.
            info["applied_config"] = {
                k: self._config.get(k) for k in ("main", "raw", "sensor", "controls")
            }
        return info

    def _manual_exposure(self) -> bool:
        return self.exposure_us is not None or self.analogue_gain is not None
