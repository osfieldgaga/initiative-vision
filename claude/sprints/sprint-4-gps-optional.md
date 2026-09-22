# Sprint 4 — GPS Logging (optional)

**Status:** Planned, awaiting your approval. Only start after Sprint 3.
**Required for Phase 1:** No (stretch)
**Depends on:** Sprint 2's OnStar finding (`docs/onstar-gps-findings.md`) and Sprint 3's session runner
**Effort:** S if OnStar works, M if a GPS module is needed

## Goal

Log geolocation with timestamps, following the same convention as frame capture and CAN. Raw position only.

## Scope (from the brief)

**In:**
- Whatever the GPS source ends up being, settled against the OnStar-via-CAN finding first
- Timestamped logging in the established convention

**Out:**
- Mapping, route processing, smoothing or any interpretation of position

## Decision gate (first task in the sprint)

Read `docs/onstar-gps-findings.md` from Sprint 2, then choose:

### Path A — OnStar GPS over CAN is usable
- No new hardware.
- **Tension with the brief's independence rule:** the brief says each stream must run on its own, but GPS from CAN can't exist without the CAN reader. I suggest making GPS a decoder over the CAN capture rather than a second reader on the same bus:
  - Simplest: no separate GPS process. The GPS CSV is produced by decoding the raw CAN log for the OnStar IDs, either at session end or by an optional side process that tails the CAN log. CAN logging never depends on it, which preserves the rule where it matters (CAN and frames stay independent of GPS).
  - Cost: GPS is available only when CAN logging is on. That's inherent to the source.
- Output: `gps.csv` with timestamp (Sprint 1 convention), latitude, longitude, plus whatever validity/quality fields the OnStar messages carry.

### Path B — dedicated GPS module
- Choose the module and interface with you (UART NMEA on the Pi's serial pins, or USB). I need to know what hardware you have or plan to buy.
- Read via `gpsd` or direct NMEA parsing. Log raw fixes (lat, lon, altitude, speed if provided, fix quality, satellites, HDOP) with a Sprint 1-convention timestamp.
- A fully independent stream, like the camera.
- **Worthwhile side benefit:** GPS provides UTC, which would give the RTC-less Pi a trustworthy wall-clock anchor. If a PPS signal is available it improves timestamp accuracy a lot. I'd raise this with you as an option and not build it in unasked, since it touches the timestamp convention you approved in Sprint 1.

## Tasks (common to both paths)

- [ ] Make the path decision from the findings, and confirm it with you before building
- [ ] Define the `gps.csv` columns and add them to the data contract (`docs/phase1-data-contract.md`)
- [ ] Log source-provided validity and quality fields as-is. If there's no fix, record that as a row with an invalid flag, not as a gap
- [ ] Session runner integration: GPS is an enabled/disabled stream like the others
- [ ] Bench test, then a short drive. Compare logged track against a known route or a phone's GPS trace for a plausibility check only (no processing built)
- [ ] Update `verify_session` for the GPS log

Path B extras:
- [ ] Hardware wiring, serial config, module configuration (update rate, message set)
- [ ] Cold-start time to first fix, in the car, documented

## Acceptance criteria

1. A drive produces a GPS CSV in the agreed convention. Positions match the route driven, checked by eye against a trace.
2. No-fix periods appear as flagged rows, not silent gaps.
3. Frame capture and CAN logging run unchanged with GPS disabled. (Path B: GPS runs unchanged with the other streams off.)
4. The data contract is updated.

## Risks

- **OnStar GPS may be low-rate or coarse** (a few Hz or less, limited resolution). That's fine for Phase 1's "raw position" goal, and the sprint reports what it is instead of hiding it.
- **Path B cold start** can be slow at the start of a short drive, leaving the first minutes without a fix.
- **GPS antenna placement** in a car needs sky view.

## Open questions for you

1. Is there GPS hardware on hand, or would Path B need a purchase?
2. If Path B: do you want GPS-derived UTC and/or PPS used to discipline the clock? That's a change to the Sprint 1 convention, so it needs your call.
