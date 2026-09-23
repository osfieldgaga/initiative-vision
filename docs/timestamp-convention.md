# Timestamp convention

**Status: proposed, needs your sign-off.** Every later stream (CAN, GPS, IMU, and Phase 2 reading the data back) depends on this being right, so it's worth reading before approving.

## The rule

Every record in every stream is timestamped as **integer nanoseconds on `CLOCK_MONOTONIC`** — the same clock `time.monotonic_ns()` reads in Python, and `clock_gettime(CLOCK_MONOTONIC)` in C. Not wall-clock time, not UTC.

Why monotonic and not wall-clock: the Pi has no RTC. At boot, its clock is wrong until NTP (or GPS) sets it, and when that correction lands, wall-clock time can jump — backward or forward, by any amount. A monotonic clock never jumps; it only ever counts up from an arbitrary reference point (typically boot, but the exact reference is not guaranteed and doesn't matter — only differences between readings are meaningful).

## What a session records

Every session's manifest (`session.json`) has a `started` and (on a clean stop) `stopped` block, each with four fields read back-to-back at that moment:

```json
{
  "monotonic_ns": 24618454185536,
  "boottime_ns": 24618454195588,
  "realtime_ns": 1790028419773714966,
  "read_span_ns": 15105
}
```

- **`monotonic_ns`** — `CLOCK_MONOTONIC`. The timeline every record's timestamp is on.
- **`boottime_ns`** — `CLOCK_BOOTTIME`. Like monotonic, but also counts time the system spent suspended. On a Pi that never suspends, the two should differ by a small, near-constant offset. Recorded so a future reader can check that assumption rather than take it on faith.
- **`realtime_ns`** — wall-clock UTC (`time.time_ns()`) at that instant. This is what lets a reader convert a session's monotonic timestamps to a real UTC date, but it inherits whatever accuracy the Pi's clock had at that moment — good after NTP has synced, unreliable before.
- **`read_span_ns`** — how long the three reads took. Bounds how tightly the three values can be trusted to correspond to "the same instant"; the realtime read is sandwiched between two monotonic reads specifically so this span is measured, not assumed. Measured at 14–15 µs on the Pi (see `clock.py`).

## Converting a session's timestamps to UTC

```
utc_ns(record) = record.timestamp_ns - session.started.monotonic_ns + session.started.realtime_ns
```

This is only as accurate as `realtime_ns` was at session start. If the Pi's clock hadn't synced with NTP yet, treat the converted times as approximate. A future improvement (not built) would be for the recorder to check and record NTP sync status at start.

## What has and hasn't been verified

**Verified, from a real session on the Pi (2026-09-22):**
- libcamera's `SensorTimestamp` (used for every frame's `timestamp_ns`) is on the `CLOCK_MONOTONIC` timeline. Measured: the first frame's timestamp is 1.168 s after the session's start anchor, matching the camera's actual startup time — not offset by some other clock's arbitrary reference.
- On this Pi, `CLOCK_BOOTTIME` and `CLOCK_MONOTONIC` agree to within 0.01 ms (no suspend has ever happened, as expected).
- Frame-to-frame timing is stable to within about ±12 µs of the nominal interval at 30 fps (measured on a 640×480 session; see `capture-performance.md`).

**Verified, from a real kernel on the Pi (2026-09-23):**
- **CAN (Sprint 2):** SocketCAN's kernel receive timestamp (`SO_TIMESTAMPNS`) is on **`CLOCK_REALTIME`**, *not* `CLOCK_MONOTONIC` — a different clock than the camera's `SensorTimestamp`. Confirmed by reading it back alongside `time.time_ns()`/`time.monotonic_ns()`/`CLOCK_BOOTTIME` at the moment of receipt on a real vcan0 socket (see `can/source.py` and the scratch probe this was measured with). Because of this, `can/source.py` does **not** use the kernel's timestamp as a CAN frame's primary `timestamp_ns` — it stamps every frame with its own `time.monotonic_ns()` read, taken immediately after `recvmsg()` returns, exactly like every other stream. The kernel's raw value is kept alongside it as `kernel_realtime_ns`, for measuring the actual read-latency gap on real hardware under real load, not for use as the primary timestamp.
- **CAN-to-frame synchronization:** solved by construction, not by a conversion step. `CLOCK_MONOTONIC` is a single, system-wide clock on Linux — shared identically by every process on the machine, not per-process. A frame-recording process and a CAN-recording process running at the same time on the same Pi therefore already share one time axis: `frame.timestamp_ns` and `can_frame.timestamp_ns` are directly comparable with zero reconciliation, even though each stream writes to its own session directory. This only holds because both recorders run on the same machine — the on-vehicle case here.

**Not yet verified — each later sprint must check this for its own source before assuming it:**
- **GPS (Sprint 4):** if a dedicated GPS module is used, its NMEA/PPS timing has its own considerations (see `sprint-4-gps-optional.md`).
- **IMU (Sprint 5):** depends entirely on which sensor and interface end up being used.

Until those are checked, don't assume any other stream's raw timestamp shares the camera's clock. Convert everything to `CLOCK_MONOTONIC` explicitly at the point each stream is read, the way the camera and CAN sources already do, rather than assuming the underlying driver's timestamp already lines up.

## Why not `CLOCK_REALTIME` (UTC) directly

It would be simpler to read and needs no conversion, but the Pi's `CLOCK_REALTIME` can jump when NTP corrects it (backward, in principle), which would show up as a timestamp going backwards or a burst of records with unrealistic gaps. `verify_session` checks that timestamps within a stream strictly increase; a wall-clock jump would either fail that check or hide a real gap, depending on which direction it jumped. Monotonic timestamps make that failure mode impossible.

## Precision and known error

- Unit: integer nanoseconds. This is finer than any of the underlying clocks' real resolution, but costs nothing to keep, and avoids a second precision-conversion step later.
- The `read_span_ns` figure (14–15 µs on the Pi) is the main known source of imprecision in the `started`/`stopped` anchors themselves. Records within a session are not affected by this — only the UTC conversion is.
- Camera-to-camera frame interval jitter: ≤12 µs (measured, see `capture-performance.md`).
