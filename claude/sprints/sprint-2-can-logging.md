# Sprint 2 — CAN Logging (read-only)

**Status:** In progress
**Required for Phase 1:** Yes
**Depends on:** Sprint 1 (approved timestamp convention and session/manifest format)
**Effort:** M–L

## Progress (2026-09-23)

Built and verified for real against a `vcan0` interface on the Pi (bench-first,
per task B, before any real hardware is involved):

- `src/ivision/can/source.py` — `SocketCanSource` (raw `CAN_RAW` socket, standard
  + extended frame decode, optional kernel ID filters, never calls `send()`) and
  `FakeCanSource` for desktop/CI testing. 29 tests, all passing on the desktop
  (6 real-interface tests skip without `vcan0`) and for real on the Pi.
- `src/ivision/can/pipeline.py` — `CanRecorder`: capture thread → bounded queue →
  writer thread, reusing `DurableCsvWriter`/`Session` from Sprint 1. Unlike frame
  capture, a quiet bus never ends the run (see "Ignition/sleep behaviour" risk,
  below) — there is deliberately no stall timeout.
- `src/ivision/can/record.py` — its own CLI entry point (task C), no shared state
  with frame capture beyond `Session`/clock helpers.
- End-to-end smoke test on the Pi against flooded `vcan0` traffic: 1770 frames
  captured/written, 0 drops, verified against kernel interface counters.

**Two findings that resolve open items above:**

1. **Timestamping (row 3 of the decisions table):** measured, not assumed.
   SocketCAN's kernel receive timestamp (`SO_TIMESTAMPNS`) is on
   `CLOCK_REALTIME`, not `CLOCK_MONOTONIC`. Every `CanFrame` is stamped with
   `time.monotonic_ns()` (this process's own read, right after `recvmsg()`
   returns) as the primary timestamp, matching every other stream's
   convention — the kernel's raw value is kept as a secondary
   `kernel_realtime_ns` field for measuring real-hardware read latency later.
2. **Task E (clock alignment):** resolved by construction, not by measuring an
   offset. `CLOCK_MONOTONIC` is one system-wide clock shared by every process
   on the Pi. A frame-recording process and a CAN-recording process running at
   the same time already share one time axis — `timestamp_ns` in both streams
   is directly comparable, no reconciliation step needed. Written up in
   `docs/timestamp-convention.md`.

**One negative result, documented rather than silently dropped:** `SO_RXQ_OVFL`
(the kernel mechanism meant to report *this process's own socket* receive-buffer
overflowing) was tested against a deliberately tiny, deliberately overflowed
`vcan0` receive buffer and reported nothing, even though the overflow was real
(500 frames sent, 3 received). Not wired into the logger since it could not be
verified to work at all — worth revisiting on real hardware via a future
`can_probe.py`. In its place, `pipeline.py` logs kernel *interface* counters
(`/sys/class/net/<iface>/statistics/rx_dropped` etc., a different, lower layer —
this is what would actually catch the "SPI CAN controller's small RX FIFO
overflows" risk called out below) at start and stop of every session, and its
own queue-drop counter catches the case where the writer/disk falls behind the
capture thread.

**One design decision made ahead of your DBC arriving, flagged for your
confirmation:** `SocketCanSource` defaults to capturing every arbitration ID
(`can_filters=None`), not just a pre-identified list. This is a deliberate
divergence from row 3 of the decisions table below ("Hardware acceptance
filters ... for the known IDs") — CAN bandwidth is trivial next to the video
stream, so logging everything means a newly-decoded signal just needs a DBC
update, never a re-drive. Hardware ID filters are still supported
(`can_filters=[(id, mask), ...]`) for the rare case bus load needs cutting.

## Goal

Log the already-identified CAN signals from the 2014 Malibu with per-record timestamps, using the same timestamp convention as the frame ledger. The sprint builds a logging pipeline around known-good signals. It does not rediscover them. It also resolves whether OnStar GPS over CAN is usable, which decides the shape of Sprint 4.

## Scope (from the brief)

**In:**
- Read and log the identified signals (wheel speed, steering angle, yaw rate, OnStar GPS) with timestamps
- A record format consistent with the frame CSV so the two can be correlated later

**Out:**
- New CAN reverse-engineering
- PSCM steering command or any actuation. **This sprint never transmits on the bus.**

## Decisions this sprint makes (with my recommendation)

| Decision | Recommendation | Why |
|---|---|---|
| Bus access mode | Listen-only / silent mode at the controller level | Guarantees the logger can't ACK, error-frame, or otherwise disturb the car's bus. Worth doing even though the code never sends. |
| What to log | Raw frames (`timestamp_ns, arb_id, dlc, data_hex`) for the identified arbitration IDs only, plus a committed signal-definition file (DBC or equivalent) describing how to decode them | Raw is lossless and keeps decoding out of Phase 1 ("no processing"). The definition file makes the log self-describing for Phase 2. **Alternative:** log decoded signal values too. Needs your call, see open questions. |
| Filtering | Hardware acceptance filters on the controller for the known IDs, with an optional "capture everything" mode for debugging | Cuts load on the SPI path and the 3B's CPU. The debug mode helps when checking a signal's ID. |
| Timestamping | Kernel receive timestamp from SocketCAN, mapped onto the Sprint 1 convention using the session anchors | Software timestamp at receive is the best a Pi + MCP2515 setup can do. Its precision and offset from the sensor clock get measured and written down, not assumed. |
| Implementation | Python + `python-can` or raw SocketCAN sockets, with the same writer/flush pattern as Sprint 1 | Reuse the proven queue/flush design. |

## Tasks

### A. Inputs from the earlier reverse-engineering work
- [ ] You provide (or point me to) the existing signal definitions: arbitration IDs, byte/bit layout, endianness, scaling and units, message rates, and which bus each signal lives on
- [ ] I convert them to one committed definition file (`can/signals.dbc` or similar) that is the single source of truth
- [ ] List any signals in the notes marked uncertain, so validation targets the right ones

### B. Hardware bring-up
- [ ] Identify the salvaged CAN hardware (SPI controller like MCP2515, or a USB adapter) and its transceiver voltage/wiring. Document wiring and the Pi overlay/config needed
- [ ] Bring up `can0` at the correct bitrate in listen-only mode
- [ ] Check the physical tap: OBD-II port vs a different tap point. **Confirm the identified signals are actually visible there**, since some vehicles filter or gateway traffic at the OBD port
- [x] Bench test without the car first: virtual CAN (`vcan`) and a replay of any existing captures, so the software is proven before it goes near the vehicle — done against `vcan0` on the Pi (29 tests + an end-to-end flood smoke test)

### C. Logger
- [x] Reader that pulls frames off `can0` into a bounded queue, with a separate writer, as in Sprint 1
- [x] CSV format with the same timestamp column definition and session directory layout as the frame log
- [x] Track and log receive-side loss: kernel *interface* counters and this pipeline's own queue-drop counter, written to the manifest at stop (see Progress note above for why not `SO_RXQ_OVFL`)
- [x] Run and stop independently of frame capture: its own entry point, no shared state or imports beyond the common timestamp/session helpers
- [x] Graceful stop and crash-safety matching Sprint 1

### D. Validation against the known signals
- [ ] `verify_can_log` script: timestamps monotonic, per-ID message rates match the expected rates, no unexplained gaps, drop counters reported
- [ ] A small offline decoder used **only for verification** (not part of the logger): drive or push the car and confirm wheel speed, steering angle and yaw rate behave sensibly. E.g. wheel speed agrees with the speedometer, and steering angle sign and range match the wheel position
- [ ] Measure achievable message throughput and CPU cost at the car's real bus load

### E. Clock alignment measurement
- [x] Quantify the offset and jitter between CAN receive timestamps and the frame sensor timestamps on the Pi, and document how it maps under the convention — resolved by construction (see Progress note above), not by measuring an offset: both streams share the same system-wide `CLOCK_MONOTONIC`. Documented in `docs/timestamp-convention.md`

### F. OnStar GPS question (settles Sprint 4)
- [ ] Decide whether the OnStar GPS signals can be decoded as usable lat/lon, and record: update rate, resolution, behaviour with no fix or ignition-off, and whether they're reachable at the tap point you use
- [ ] Write the finding as `docs/onstar-gps-findings.md` with a clear conclusion: **usable** (Sprint 4 needs no GPS module), **partially usable** (details), or **not usable** (Sprint 4 needs a dedicated module)

## Acceptance criteria

1. A capture from the real car produces a CSV of timestamped records for all identified IDs. `verify_can_log` reports no unexplained gaps and per-ID rates within tolerance of expected.
2. The logger has been shown to never transmit (listen-only mode confirmed; I'll check for any TX frames on a second observer or the controller status).
3. Offline decode of a real drive shows wheel speed, steering angle and yaw rate consistent with what the car did.
4. A short concurrent run with Sprint 1's frame capture (a few minutes on the Pi) completes with neither stream failing. The full concurrent acceptance test is Sprint 3.
5. `docs/onstar-gps-findings.md` gives a clear yes/partial/no.
6. The measured CAN-vs-camera clock offset is documented.
7. CAN logging starts and stops with the camera and every other stream absent.

## Risks

- **The OBD port may not expose the reverse-engineered signals** (gateway filtering, or signals living on another bus). Tap-point check is early in the sprint for that reason.
- **SPI CAN controllers have small RX buffers.** At high bus load and a busy 3B, frames can overflow. Hardware filters and drop counters are the mitigation and the detection.
- **A loose CAN tap could disturb the car.** Mitigated by listen-only mode and a bench-first workflow. I'd also advise keeping the first vehicle tests short.
- **Signal definitions from the old project may be incomplete or undocumented**, costing time here. I'll flag any gaps back to you rather than guess.
- **Ignition/sleep behaviour.** The bus goes quiet when the car sleeps, so the logger has to handle silence, not treat it as a fault.

## Open questions for you

1. Where are the existing signal definitions, and in what format?
2. What CAN hardware do you have (which controller/adapter, transceiver, cabling)?
3. Log raw frames only, or also a decoded signals CSV? I recommend raw plus the definition file, but decoded values may be what you actually want to look at.
4. Where will the car tap be (OBD-II port or elsewhere)?
5. Can I get a real drive capture to test against, or will there be one from you during the sprint?

## Hand-off to Sprint 3

A CAN logger with its own entry point, the CAN log format, the measured CAN/camera clock relationship, and the OnStar GPS finding.
