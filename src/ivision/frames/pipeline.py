"""Capture pipeline: source -> (encode) -> bounded queue -> writer thread.

Design rules:

* A slow disk or slow encoder must never stall the sensor. The capture thread
  only does non-blocking hand-offs; if there is no room the frame is dropped and
  counted. Dropped frames keep their ``seq``, so they show up as gaps in the
  ledger. Two separate counters say *why*: ``frames_dropped_encode`` (encoder
  pool saturated) and ``frames_dropped_queue`` (writer/disk backlog).
* If the source has an ``encode(frame)`` method, it runs inline in the capture
  thread (``encode_workers=0``) or in a pool of worker threads. The queue keeps
  frames in capture order either way, so the ledger stays in order.
* A ledger row is written only after its frame file is complete on disk
  (written to ``*.tmp``, then renamed), so a row never points at a missing or
  half-written file. File contents are not fsync'd by default; ``verify``
  checks JPEG start/end markers to catch a torn file after a power cut.
* Each frame's filename embeds its own timestamp (``<seq>_<timestamp_ns>.<ext>``,
  2026-09-22). The ledger is then redundant with the files: if it is lost or
  badly torn, ``frames.rebuild`` reconstructs it from the filenames alone. This
  matters because a slow disk (see ``disk_bench``) means the ledger is flushed
  and fsynced far less often than every row now.
* On a storage error the writer records it, stops the source, and drains the
  queue. A source error stops capture but the frames already queued are still
  written. Either way the session closes cleanly and says why it stopped.
"""

from __future__ import annotations

import dataclasses
import os
import queue
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from ..csvlog import DurableCsvWriter
from ..session import Session
from .source import Frame, FrameSource

COLUMNS = ("seq", "filename", "timestamp_ns")
_SENTINEL = object()


_NAME_RE = re.compile(r"^(\d{8})_(\d+)\.(\w+)$")


def frame_relpath(frame: Frame) -> str:
    return f"frames/{frame.seq:08d}_{frame.timestamp_ns}.{frame.ext}"


def parse_frame_filename(name: str):
    """'00000005_123456789.jpg' -> (5, 123456789, 'jpg'), or None if it doesn't match."""
    m = _NAME_RE.match(name)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), m.group(3)


def _lower_priority(nice: int) -> None:
    """Make the calling thread nicer. Linux nice values are per thread.

    Encoders get the CPU the camera and capture loop leave over. Without this,
    heavy encoding starved libcamera on the Pi 3B (2026-09-21) and the camera
    stalled; with it, overload turns into counted dropped frames instead.
    """
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), nice)
    except (OSError, AttributeError):
        pass  # not Linux, or not permitted: run at normal priority


def write_frame_file(path: Path, data: bytes, fsync: bool = False) -> None:
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as f:
        f.write(data)
        if fsync:
            f.flush()
            os.fsync(f.fileno())
    os.replace(tmp, path)


class Recorder:
    def __init__(
        self,
        session: Session,
        source: FrameSource,
        queue_size: int = 64,
        fsync_files: bool = False,
        file_writer: Callable[[Path, bytes, bool], None] = write_frame_file,
        flush_interval_s: float = 0.5,
        fsync_interval_s: float = 1.0,
        encode_workers: int = 0,
        max_inflight: int | None = None,
        stop_timeout_s: float = 5.0,
        stall_timeout_s: float | None = 5.0,
        encode_nice: int | None = 10,
    ):
        self.session = session
        self.source = source
        self.fsync_files = fsync_files
        self.encode_workers = encode_workers
        # Raw frames alive at once = in-flight encodes. This bounds memory.
        self.max_inflight = max_inflight or max(1, encode_workers * 2)
        self._encode_nice = encode_nice
        self._stop_timeout_s = stop_timeout_s
        # No frame for this long ends the run with a clear reason (None = off).
        self._stall_timeout_s = stall_timeout_s
        self._capture_started_mono: float | None = None
        self._last_frame_mono: float | None = None
        self.source_start_s: float | None = None
        self._encode = getattr(source, "encode", None)
        self._pool: ThreadPoolExecutor | None = None
        self._inflight = threading.BoundedSemaphore(self.max_inflight)
        self._file_writer = file_writer
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._flush_interval_s = flush_interval_s
        self._csv = DurableCsvWriter(
            session.frames_csv,
            COLUMNS,
            flush_interval_s=flush_interval_s,
            fsync_interval_s=fsync_interval_s,
        )
        self._stop_requested = threading.Event()
        self._write_error: str | None = None
        self._source_error: str | None = None
        self.captured = 0
        self.last_seq_captured: int | None = None
        self.dropped = 0
        self.dropped_encode = 0
        self.written = 0
        self.unwritten_after_error = 0
        self.last_seq_written: int | None = None

        session.set_stream(
            "frames",
            csv=session.frames_csv.name,
            directory="frames",
            columns=list(COLUMNS),
            status="recording",
        )
        self._refresh_camera_manifest()

    def _refresh_camera_manifest(self) -> None:
        self.session.update_camera(**self.source.describe())
        self.session.flush()

    def request_stop(self) -> None:
        """Ask the recorder to finish. Safe from a signal handler or thread."""
        self._stop_requested.set()

    def run(self) -> dict:
        """Record until stopped, the source ends, or something fails."""
        started = time.monotonic()
        try:
            self.source.start()
        except Exception as exc:
            self.source_start_s = round(time.monotonic() - started, 3)
            self._source_error = f"source_error: start failed: {exc.__class__.__name__}: {exc}"
            self._close_source()
            return self._finish()
        self.source_start_s = round(time.monotonic() - started, 3)
        # The source knows more about itself once it is running (actual sensor
        # mode, crop, exposure), and calibration depends on those.
        self._refresh_camera_manifest()

        if self._encode is not None and self.encode_workers > 0:
            self._pool = ThreadPoolExecutor(
                max_workers=self.encode_workers,
                thread_name_prefix="encode",
                initializer=_lower_priority if self._encode_nice else None,
                initargs=(self._encode_nice,) if self._encode_nice else (),
            )
        # Daemon threads: a hung camera driver must not keep the process alive.
        capture = threading.Thread(target=self._capture_loop, name="capture", daemon=True)
        writer = threading.Thread(target=self._writer_loop, name="writer", daemon=True)
        self._capture_started_mono = time.monotonic()
        capture.start()
        writer.start()

        while capture.is_alive() and not self._stop_requested.is_set():
            capture.join(0.05)
            if self._stalled():
                self._source_error = self._source_error or (
                    f"source_error: no frame for {self._stall_timeout_s}s "
                    f"(camera stalled or disconnected; {self.captured} frames received)"
                )
                break
        self.source.stop()
        capture.join(self._stop_timeout_s)
        if capture.is_alive():
            self._source_error = self._source_error or (
                f"source_error: capture did not stop within {self._stop_timeout_s}s"
            )
            # The thread is still blocked inside the source (e.g. a libcamera call that
            # will never return), so this close races it. Best-effort: leaking the camera's
            # DMA buffers (CMA) is worse than a possibly-unsafe close, and Picamera2's
            # stop/close are meant to be callable at any time. Found 2026-09-22: skipping
            # close() here after a stall left the Pi's CMA pool nearly exhausted and every
            # later camera start failed until the Pi was rebooted.
        self._close_source()
        self._queue.put(_SENTINEL)  # writer always drains, so this cannot hang
        writer.join()
        if self._pool is not None:
            self._pool.shutdown(wait=True)

        return self._finish()

    def _stalled(self) -> bool:
        if self._stall_timeout_s is None or self._capture_started_mono is None:
            return False
        last = self._last_frame_mono or self._capture_started_mono
        return time.monotonic() - last > self._stall_timeout_s

    @property
    def queue_depth(self) -> int:
        """Frames waiting for the disk right now. Climbing toward the queue size means the
        disk is not keeping up and drops are coming."""
        return self._queue.qsize()

    @property
    def queue_capacity(self) -> int:
        return self._queue.maxsize

    @property
    def seconds_since_frame(self) -> float | None:
        """Seconds since the last frame arrived, or None if none has yet."""
        return None if self._last_frame_mono is None else time.monotonic() - self._last_frame_mono

    def _close_source(self) -> None:
        try:
            self.source.close()
        except Exception as exc:
            self._source_error = self._source_error or (
                f"source_error: close failed: {exc.__class__.__name__}: {exc}"
            )

    # -- capture thread ----------------------------------------------------

    def _capture_loop(self) -> None:
        try:
            for frame in self.source.frames():
                self.captured += 1
                self.last_seq_captured = frame.seq
                self._last_frame_mono = time.monotonic()
                item = self._prepare(frame)
                if item is not None:
                    try:
                        self._queue.put_nowait(item)
                    except queue.Full:
                        self.dropped += 1
                if self._write_error is not None:
                    self.source.stop()
                    return
        except Exception as exc:  # a crashing source must not look like a clean end
            self._source_error = f"source_error: {exc.__class__.__name__}: {exc}"

    def _prepare(self, frame: Frame):
        """Return what goes on the queue (a Frame or a Future), or None if dropped."""
        if self._encode is None:
            return frame
        if self._pool is None:
            return dataclasses.replace(frame, data=self._encode(frame))
        if not self._inflight.acquire(blocking=False):
            self.dropped_encode += 1
            return None
        if self._queue.full():  # don't spend encode time on a frame with no room
            self._inflight.release()
            self.dropped += 1
            return None
        return self._pool.submit(self._encode_job, frame)

    def _encode_job(self, frame: Frame) -> Frame:
        try:
            return dataclasses.replace(frame, data=self._encode(frame))
        finally:
            self._inflight.release()

    # -- writer thread -----------------------------------------------------

    def _writer_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=self._flush_interval_s)
            except queue.Empty:
                self._tick_csv()
                continue
            if item is _SENTINEL:
                return
            if self._write_error is not None:
                self.unwritten_after_error += 1
                continue
            try:
                frame = item.result() if isinstance(item, Future) else item
                path = self.session.root / frame_relpath(frame)
                self._file_writer(path, frame.data, self.fsync_files)
                self._csv.write_row([frame.seq, frame_relpath(frame), frame.timestamp_ns])
            except Exception as exc:
                # Keep draining after a failure so the shutdown put() cannot block.
                kind = "storage_error" if isinstance(exc, OSError) else "writer_error"
                self._write_error = f"{kind}: {exc.__class__.__name__}: {exc}"
                self.unwritten_after_error += 1
                continue
            self.written += 1
            self.last_seq_written = frame.seq

    def _tick_csv(self) -> None:
        try:
            self._csv.tick()
        except OSError as exc:
            self._write_error = self._write_error or f"storage_error: {exc.__class__.__name__}: {exc}"

    # -- shutdown ----------------------------------------------------------

    def _finish(self) -> dict:
        try:
            self._csv.close()
        except OSError as exc:
            self._write_error = self._write_error or f"storage_error: {exc.__class__.__name__}: {exc}"

        if self._write_error:
            reason = self._write_error
        elif self._source_error:
            reason = self._source_error
        elif self._stop_requested.is_set():
            reason = "requested"
        else:
            reason = "source_ended"

        summary = {
            "status": "complete",
            "stop_reason": reason,
            "frames_captured": self.captured,
            "last_seq_captured": self.last_seq_captured,
            "frames_written": self.written,
            "frames_dropped_queue": self.dropped,
            "frames_dropped_encode": self.dropped_encode,
            "frames_unwritten_after_error": self.unwritten_after_error,
            "last_seq_written": self.last_seq_written,
            "encode_workers": self.encode_workers,
            "source_start_s": self.source_start_s,
        }
        self.session.set_stream("frames", **summary)
        try:
            self.session.finalize()
        except OSError as exc:
            # Disk full can prevent even the manifest write; report it.
            summary["manifest_error"] = str(exc)
        return summary
