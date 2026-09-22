"""Crash-tolerant CSV writer shared by every stream.

Policy, so an unclean power cut loses at most the last few rows and never
corrupts earlier ones:

* Rows are buffered in memory and written with whole-line ``os.write`` calls,
  so the file only ever ends in a complete line or a torn final line.
* Buffered rows are written every ``flush_interval_s``.
* The file is fsync'd every ``fsync_interval_s``.

The caller must call ``tick()`` when idle (a queue timeout is enough), or the
last rows sit in memory until ``close()``.
"""

from __future__ import annotations

import csv
import io
import os
import time
from pathlib import Path
from typing import Callable, Sequence


class DurableCsvWriter:
    def __init__(
        self,
        path,
        columns: Sequence[str],
        flush_interval_s: float = 0.5,
        fsync_interval_s: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.path = Path(path)
        self.columns = tuple(columns)
        self.flush_interval_s = flush_interval_s
        self.fsync_interval_s = fsync_interval_s
        self._clock = clock
        # O_EXCL: never overwrite or append to an existing log.
        self._fd = os.open(
            self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND, 0o644
        )
        self._buf = io.StringIO()
        self._csv = csv.writer(self._buf, lineterminator="\n")
        self._pending: list = []
        self._dirty = False
        self._rows = 0
        self._closed = False

        self._pending.append(self._format(self.columns))
        self._write_pending()
        self._fsync()
        now = self._clock()
        self._last_flush = now
        self._last_fsync = now

    @property
    def rows_written(self) -> int:
        """Rows accepted so far, including any still buffered in memory."""
        return self._rows

    def write_row(self, values: Sequence) -> None:
        if self._closed:
            raise ValueError("write to closed CsvWriter")
        if len(values) != len(self.columns):
            raise ValueError(
                f"expected {len(self.columns)} values, got {len(values)}"
            )
        self._pending.append(self._format(values))
        self._rows += 1
        self.tick()

    def tick(self) -> None:
        now = self._clock()
        if self._pending and now - self._last_flush >= self.flush_interval_s:
            self._write_pending()
            self._last_flush = now
        if self._dirty and now - self._last_fsync >= self.fsync_interval_s:
            self._fsync()
            self._last_fsync = now

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._write_pending()
            self._fsync()
        finally:
            self._closed = True
            os.close(self._fd)

    def _format(self, values: Sequence) -> str:
        self._buf.seek(0)
        self._buf.truncate()
        self._csv.writerow(values)
        return self._buf.getvalue()

    def _write_pending(self) -> None:
        if not self._pending:
            return
        data = "".join(self._pending).encode("utf-8")
        view = memoryview(data)
        while view:
            n = os.write(self._fd, view)
            view = view[n:]
        self._pending.clear()
        self._dirty = True

    def _fsync(self) -> None:
        os.fsync(self._fd)
        self._dirty = False
