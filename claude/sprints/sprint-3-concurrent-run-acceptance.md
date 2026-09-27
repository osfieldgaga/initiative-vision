# Sprint 3 — Concurrent Run + Phase 1 Acceptance

**Status:** In progress, started 2026-09-27. Resource-budget measurement done (task B); real vehicle drive test, cross-stream validation, and the data contract doc are still open (tasks C-E). Runner (task A) deliberately kept manual per your decision, not built as a launcher/systemd unit
**Required for Phase 1:** Yes. **Completing this sprint completes Phase 1.**
**Depends on:** Sprints 1 and 2
**Effort:** M

## Why this sprint exists

The brief defines Phase 1 as done when "CAN data is flowing and logged alongside frame capture." Sprints 1 and 2 prove each stream alone. Nothing yet proves they work together on a Pi 3B in a car. Frame capture is a heavy load on CPU and storage, and the CAN logger needs steady, low-latency service. This sprint proves the combination and writes down what Phase 1 hands to Phase 2.

## Goal

Run frame capture and CAN logging together for a realistic drive on the Pi 3B, in the car, and produce a verified session. Write the data contract that Phase 2 will consume.

## Scope

**In:**
- A minimal way to start and stop all streams together into one session directory
- A thermal, power and storage budget for a real drive
- A full in-vehicle test and post-run validation
- The Phase 1 data contract document

**Out:**
- Correlation, alignment or fusion logic. Validation only checks that streams share a timeline. It does not produce merged data
- The BLE companion app (separate track). Start/stop here is CLI and/or systemd only
- Any cloud upload (see Sprint 6)

## Tasks

### A. Session runner
- [ ] A small launcher that creates one session directory and manifest, then starts each enabled stream as its own independent process. It writes the shared clock anchors once
- [ ] Streams stay independent: if the CAN process dies, frame capture keeps going, and vice versa. The runner reports the failure and doesn't restart it in a way that could corrupt a log
- [ ] `status` output: per-stream rows written, current rate, drops so far, disk free, SoC temperature
- [ ] Clean start/stop from the command line, plus a systemd unit so it can start on boot. Decide with you whether auto-start-on-boot is wanted (see open questions)
- [ ] Enabled streams are configurable, so Sprints 4 and 5 plug in without changes to the runner's structure

### B. Resource budget on the real hardware
- [x] Measure combined CPU, memory, SD/USB write throughput and SoC temperature with both streams running. Compare against the Sprint 1 solo numbers to see what CAN costs the camera — **measured 2026-09-27, real vehicle bus, two runs:** frame drops 16.5% and 20.9% (vs ~0.64% solo), CAN queue drops 11% and 2.17% (vs ~0% solo). Confirmed real, reproduced twice; not thread-count-dependent (tried 1280x1280 at both 2 and 3 encoder threads — 2 threads alone, no CAN, already dropped 22.6%, ruling out "fewer CAN-competing threads" as the fix)
- [x] If the camera drops frames when CAN is active, adjust priorities... — **Decided 2026-09-27: deferred, not fixed.** Two real candidate causes identified but not isolated (CPU/scheduling contention between JPEG encoding and CAN's threads; SD card I/O contention from ~2,200 CAN writes/sec landing alongside frame writes). Time-constrained (competition deadline) — accepted as a known, documented trade-off rather than blocking on it: **real drives proceed at 1280x1280/3 threads with this drop rate accepted**, revisit if it proves to matter once real Phase 2 processing is underway
- [ ] Storage budget: GB per hour at the chosen operating point, drive length the storage supports, and the behaviour when it fills (stop cleanly, keep the earlier data intact)
- [ ] Power: the Pi runs from a **battery / power bank** (your answer), so it isn't exposed to cranking or ignition-off transients. The risks are different: check the Pi's undervoltage/throttle flags under full load (a power bank or its cable that sags below ~4.8V throttles the Pi and can drop frames), confirm the bank doesn't cut output at low draw or under load spikes, and estimate runtime at the measured draw so a session can't outlast the battery. Test an abrupt cut (pull the cable) as well. The goal is logs that survive an abrupt power loss with only the trailing rows missing
- [ ] Battery-low behaviour: the bank will eventually cut power with no warning. Decide whether to just accept that (and rely on the crash-safety above) or add a low-battery check. I'd start with the first

### C. Vehicle test
- [ ] At least one 30+ minute drive covering stops, turns and speed changes, with both streams on
- [ ] A repeat on a hot day or a long session if practical, to check thermal throttling doesn't degrade the capture rate
- [ ] Bench-check any changes first; I'd propose a short test before each long one

### D. Post-run validation (checks only, no data products)
- [ ] `verify_session` extended across streams: each log is internally sound, and all streams sit on the same timeline (start/stop times overlap as expected, no clock jumps between anchors and later records)
- [ ] Physical sanity check across streams: for example a speed change or turn visible in the CAN wheel speed or steering angle lines up in time with the video (spot-check by eye on a few events). This confirms the timestamp convention works. It does not build correlation tooling
- [ ] Record the measured cross-stream timing error bound

### E. Phase 1 data contract
- [ ] Write `docs/phase1-data-contract.md`: session directory layout, every file and column, units, timestamp convention and anchors, known error bounds, drop accounting, camera calibration status, signal-definition file. **This is the only thing Phase 1 owes Phase 2**, so it should be complete enough for someone (or me) to write the Phase 2 reader from it alone
- [ ] Include a short "known limitations" list from what the tests showed (achieved fps, CAN loss rate, clock error)

## Acceptance criteria

1. One 30+ minute in-vehicle session with both streams produces a session that passes cross-stream verification.
2. Camera drop rate and CAN loss rate are measured and documented for the combined run. Any degradation versus solo runs is explained.
3. Killing one stream leaves the other running and its log intact.
4. A sudden power cut mid-session leaves logs that load, missing at most the trailing records.
5. `docs/phase1-data-contract.md` is written and you've reviewed it.
6. The brief's Phase 1 definition of done is met, and I'll state that explicitly in the sprint report.

## Risks

- **CPU/IO contention degrades one stream.** The likeliest finding. The plan above measures it first and treats a lower frame rate as your decision.
- **Power bank behaviour.** No 12V transients to worry about, but banks can sag under load, auto-shut-off, or die mid-drive without warning. Mitigated by the undervoltage check and the crash-safe logs. A weak bank or cable is hardware outside my control.
- **SD card wear or corruption** from constant heavy writes and hard power-offs. This sprint is planned on the SD card (per your decision), so the risk is real. The USB SSD you're buying is the fix, and the in-car test is best run on it if it has arrived. If it hasn't, I'd keep the drive test short and back up sessions promptly.
- **Test conditions.** Driving time is yours to schedule, and the sprint can't close without it.

## Open questions for you

1. ~~How is the Pi powered?~~ Answered: battery / power bank. **Still open:** the bank's make, capacity and output rating (helps the runtime estimate).
2. Should the runner auto-start on boot, or only start manually? With a power bank there's no ignition signal, so "boot" means "when you plug it in or switch it on". Auto-start on boot is easy. Auto-start on ignition would need extra hardware.
3. When and where can you do a 30+ minute drive test?
4. Is a hot-weather thermal test possible before Phase 1 closes?

## Hand-off

Phase 1 complete. Sprints 4 and 5 (optional) extend the runner with additional streams, and Sprint 6 (needs your decision) covers getting sessions off the device.
