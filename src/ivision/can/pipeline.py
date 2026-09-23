"""Capture pipeline: source -> bounded queue -> writer thread.

Mirrors ``frames/pipeline.py``'s shape (capture thread hands off to a bounded
queue; a writer thread drains it; a full queue drops and counts, never
blocks), minus the encode stage -- a CAN frame is already a complete, tiny
record, there is nothing to encode.

One deliberate difference from frames: **a quiet bus is not a fault.** The
frame pipeline treats "no frame for N seconds" as a stalled/disconnected
camera and ends the run. On a vehicle, the CAN bus goes silent whenever the
ignition is off or the car sleeps -- that is the expected, common case, not
an error. ``CanRecorder`` therefore has no stall timeout at all: it runs
until asked to stop, however long the bus stays quiet.

Synchronization with the frame stream (see ``can/source.py``'s module
docstring for the clock finding): both pipelines timestamp with
``time.monotonic_ns()``, and ``CLOCK_MONOTONIC`` is a single, system-wide
clock on Linux -- shared by every process on the machine, not per-process.
So a frame session and a CAN session recorded at the same time on the same
Pi are already on one common time axis, with no reconciliation step needed,
even though each writes to its own session directory. This only holds when
both run on the same machine, which is the on-vehicle case here.

Drop accounting has two layers, deliberately not conflated:

* ``dropped`` (this pipeline's own queue): the writer/disk fell behind and a
  frame already read from the socket had nowhere to go. Same meaning as
  ``frames_dropped_queue``.
* ``if_stats`` (kernel interface counters, ``/sys/class/net/<iface>/statistics/``):
  drops *before* this process ever saw the frame -- e.g. a small SPI CAN
  controller's hardware RX FIFO overflowing under bus load, the risk the
  sprint plan flagged. Captured at start and stop and put in the manifest.
  This is not the same thing ``SO_RXQ_OVFL`` would report (that is a third,
  narrower layer: this process's own socket receive buffer overflowing
  between kernel delivery and our ``recvmsg()``) -- tested against vcan0
  2026-09-23 and found to report nothing even under a confirmed, deliberate
  overflow (500 frames sent into a 512-byte SO_RCVBUF, only 3 received, kernel
  interface counters correctly showed 500 arrivals, but SO_RXQ_OVFL's
  ancillary data never appeared). Not wired in here since it could not be
  verified to work at all; worth revisiting once real hardware is available
  (see ``can_probe.py``). In practice this middle layer's risk is low: the
  capture thread does almost no work per message (recvmsg, one timestamp,
  one queue put), so it should rarely fall behind the kernel even though the
  writer/disk might fall behind the capture thread -- which is exactly what
  ``dropped`` above already catches.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path

from ..csvlog import DurableCsvWriter
from ..session import Session
from .source import CanFrame, CanSource

COLUMNS = ("seq", "timestamp_ns", "kernel_realtime_ns", "can_id", "is_extended",
           "is_remote_frame", "dlc", "data")
_SENTINEL = object()

_IF_STAT_NAMES = ("rx_packets", "rx_dropped", "rx_errors", "rx_over_errors", "rx_fifo_errors")


def read_if_stats(interface: str) -> dict | None:
    """Kernel network-interface counters for ``interface``, or None if they
    can't be read (no such interface, not Linux, permissions). Best-effort:
    a missing counter must never break a recording."""
    stats = {}
    for name in _IF_STAT_NAMES:
        try:
            with open(f"/sys/class/net/{interface}/statistics/{name}") as f:
                stats[name] = int(f.read().strip())
        except OSError:
            return None
    return stats


def _format_row(seq: int, frame: CanFrame) -> list:
    return [
        seq,
        frame.timestamp_ns,
        frame.kernel_realtime_ns if frame.kernel_realtime_ns is not None else "",
        f"0x{frame.can_id:X}",
        int(frame.is_extended),
        int(frame.is_remote_frame),
        frame.dlc,
        frame.data.hex(),
    ]


class CanRecorder:
    def __init__(
        self,
        session: Session,
        source: CanSource,
        queue_size: int = 4096,
        flush_interval_s: float = 0.5,
        fsync_interval_s: float = 1.0,
        stop_timeout_s: float = 5.0,
    ):
        self.session = session
        self.source = source
        self._stop_timeout_s = stop_timeout_s
        self._queue: queue.Queue = queue.Queue(maxsize=queue_size)
        self._csv = DurableCsvWriter(
            self._can_csv_path(session),
            COLUMNS,
            flush_interval_s=flush_interval_s,
            fsync_interval_s=fsync_interval_s,
        )
        self._stop_requested = threading.Event()
        self._write_error: str | None = None
        self._source_error: str | None = None
        self.captured = 0
        self.dropped = 0
        self.written = 0
        self.unwritten_after_error = 0
        self._if_stats_before: dict | None = None
        self._if_stats_after: dict | None = None

        session.set_stream(
            "can",
            csv=self._can_csv_path(session).name,
            columns=list(COLUMNS),
            status="recording",
            **source.describe(),
        )
        session.flush()

    @staticmethod
    def _can_csv_path(session: Session) -> Path:
        return session.root / "can.csv"

    def request_stop(self) -> None:
        """Ask the recorder to finish. Safe from a signal handler or thread."""
        self._stop_requested.set()

    @property
    def queue_depth(self) -> int:
        return self._queue.qsize()

    @property
    def queue_capacity(self) -> int:
        return self._queue.maxsize

    def run(self) -> dict:
        """Record until stopped or something fails. Never ends on its own
        just because the bus has gone quiet -- see the module docstring."""
        interface = getattr(self.source, "interface", None)
        if interface is not None:
            self._if_stats_before = read_if_stats(interface)
        try:
            self.source.start()
        except Exception as exc:
            self._source_error = f"source_error: start failed: {exc.__class__.__name__}: {exc}"
            self._close_source()
            return self._finish()

        capture = threading.Thread(target=self._capture_loop, name="can-capture", daemon=True)
        writer = threading.Thread(target=self._writer_loop, name="can-writer", daemon=True)
        capture.start()
        writer.start()

        while capture.is_alive() and not self._stop_requested.is_set():
            capture.join(0.1)
        self.source.stop()
        capture.join(self._stop_timeout_s)
        if capture.is_alive():
            self._source_error = self._source_error or (
                f"source_error: capture did not stop within {self._stop_timeout_s}s"
            )
        self._close_source()
        if interface is not None:
            self._if_stats_after = read_if_stats(interface)
        self._queue.put(_SENTINEL)  # writer always drains, so this cannot hang
        writer.join()
        return self._finish()

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
                try:
                    self._queue.put_nowait((self.captured, frame))
                except queue.Full:
                    self.dropped += 1
                if self._write_error is not None:
                    self.source.stop()
                    return
        except Exception as exc:  # a crashing source must not look like a clean end
            self._source_error = f"source_error: {exc.__class__.__name__}: {exc}"

    # -- writer thread -----------------------------------------------------

    def _writer_loop(self) -> None:
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except queue.Empty:
                self._tick_csv()
                continue
            if item is _SENTINEL:
                return
            if self._write_error is not None:
                self.unwritten_after_error += 1
                continue
            seq, frame = item
            try:
                self._csv.write_row(_format_row(seq, frame))
            except OSError as exc:
                self._write_error = f"storage_error: {exc.__class__.__name__}: {exc}"
                self.unwritten_after_error += 1
                continue
            self.written += 1

    def _tick_csv(self) -> None:
        try:
            self._csv.tick()
        except OSError as exc:
            self._write_error = self._write_error or f"storage_error: {exc.__class__.__name__}: {exc}"

    # -- shutdown ------------------------------------------------------------

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
            "can_frames_captured": self.captured,
            "can_frames_written": self.written,
            "can_frames_dropped_queue": self.dropped,
            "can_frames_unwritten_after_error": self.unwritten_after_error,
            "if_stats_before": self._if_stats_before,
            "if_stats_after": self._if_stats_after,
        }
        self.session.set_stream("can", **summary)
        try:
            self.session.finalize()
        except OSError as exc:
            summary["manifest_error"] = str(exc)
        return summary
