"""Clock anchors for the session manifest.

PROPOSED timestamp convention (Sprint 1, task C; not yet approved):
every stream timestamps records in integer nanoseconds on one monotonic
clock, and each session records one set of anchors so a reader can map that
clock to UTC. The Pi has no RTC, so wall time can be wrong or can jump when
NTP/GPS sets it; monotonic time never does.

Which kernel clock the libcamera sensor timestamp uses must still be verified
on the Pi. That decides which anchor field the frame timestamps refer to.
"""

from __future__ import annotations

import time


def read_anchors() -> dict:
    """Read the clocks back-to-back and report how long the reads took.

    ``read_span_ns`` bounds the error of pairing the three clocks: they were
    all read within that window. The realtime read is sandwiched between two
    monotonic reads so the span is measured, not assumed.
    """
    mono_before = time.monotonic_ns()
    boottime = time.clock_gettime_ns(time.CLOCK_BOOTTIME)
    realtime = time.time_ns()
    mono_after = time.monotonic_ns()
    return {
        "monotonic_ns": mono_before,
        "boottime_ns": boottime,
        "realtime_ns": realtime,
        "read_span_ns": mono_after - mono_before,
    }
