"""Frame sources.

A source yields ``Frame`` objects. ``Frame.data`` is either finished image
bytes (fake source) or raw pixel data that the source's own ``encode(frame)``
turns into bytes (camera source). The recorder runs ``encode`` inline or in a
worker pool, chosen with its ``encode_workers`` setting, because where JPEG
encoding runs is what the Pi throughput spike has to measure.

``seq`` counts frames the sensor delivered to the app, starting at 0.
``timestamp_ns`` is the capture time on the session's monotonic clock.
"""

from __future__ import annotations

import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Protocol


@dataclass(frozen=True)
class Frame:
    seq: int
    timestamp_ns: int
    data: Any  # bytes when finished; raw pixels until the source's encode() runs
    ext: str = "jpg"


class FrameSource(Protocol):
    def describe(self) -> dict:
        """Camera/capture config for the manifest."""

    def start(self) -> None: ...

    def stop(self) -> None:
        """Ask ``frames()`` to end. Must be safe to call from another thread."""

    def frames(self) -> Iterator[Frame]: ...

    def close(self) -> None:
        """Release hardware. Called once, after ``frames()`` has finished."""

    # Optional: ``encode(frame) -> bytes``. If present, the recorder calls it
    # to turn a raw frame into file bytes (inline or in a worker pool).


def synthetic_jpeg(seq: int, payload_bytes: int = 2048) -> bytes:
    """Bytes with JPEG start/end markers. NOT a decodable image."""
    return b"\xff\xd8" + seq.to_bytes(8, "big") + bytes(payload_bytes) + b"\xff\xd9"


class FakeCameraSource:
    """Deterministic stand-in for the camera, for tests and desktop runs."""

    def __init__(
        self,
        fps: float = 30.0,
        num_frames: int | None = None,
        payload_bytes: int = 2048,
        jitter_ns: int = 0,
        realtime: bool = True,
        start_ns: int | None = None,
        seed: int = 0,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ):
        self.fps = fps
        self.num_frames = num_frames
        self.payload_bytes = payload_bytes
        self.jitter_ns = jitter_ns
        self.realtime = realtime
        self.start_ns = start_ns
        self._rng = random.Random(seed)
        self._clock_ns = clock_ns
        self._stop = threading.Event()

    def describe(self) -> dict:
        return {
            "kind": "fake",
            "synthetic": True,
            "target_fps": self.fps,
            "resolution": None,
        }

    def start(self) -> None:
        self._stop.clear()

    def stop(self) -> None:
        self._stop.set()

    def close(self) -> None:
        pass

    def frames(self) -> Iterator[Frame]:
        interval_ns = round(1e9 / self.fps)
        if self.jitter_ns * 2 >= interval_ns:
            raise ValueError("jitter must stay under half the frame interval")
        t = self.start_ns if self.start_ns is not None else self._clock_ns()
        seq = 0
        while not self._stop.is_set():
            if self.num_frames is not None and seq >= self.num_frames:
                return
            if self.realtime:
                wait_s = (t - self._clock_ns()) / 1e9
                if wait_s > 0 and self._stop.wait(wait_s):
                    return
            jitter = self._rng.randint(-self.jitter_ns, self.jitter_ns) if self.jitter_ns else 0
            yield Frame(seq, t + jitter, synthetic_jpeg(seq, self.payload_bytes))
            seq += 1
            t += interval_ns
