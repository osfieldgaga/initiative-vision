# Initiative Vision 1 — Phase 1 (Acquisition): Project Description

**Purpose of this document:** this is a scope and intent brief, not a sprint plan. Read it, then define sprint objectives and concrete tasks from it — start with Frame Capture.

---

## Where this fits

Initiative Vision 1 has two phases:

- **Phase 1 — Acquisition (this document).** Runs entirely on-vehicle, on the SBC. Captures raw data streams — camera frames, CAN, GPS, IMU — each logged with a timestamp. No processing, no inference. The only cloud step is the device-side upload of finished sessions to S3 (Sprint 6, added 2026-09-21).
- **Phase 2 — Processing.** Runs in AWS (OpenCV 5, COOL on Graviton). Out of scope here — don't design around it, don't build toward it yet. The only thing Phase 1 owes Phase 2 is clean, well-timestamped data that can be correlated across streams later.

This acquisition/processing split is also the architecture behind the "Initiative Vision" hackathon entry (OpenCV AI Competition 2026) — the edge device collects, AWS processes. Phase 1 is that edge side.

**Vehicle platform:** 2014 Chevrolet Malibu. There's already a substantial CAN bus reverse-engineering project on this car — signals for wheel speed, steering angle, yaw rate, and GPS (via OnStar) are already identified. The CAN sprint builds on that work, not from zero.

**Current hardware:** Raspberry Pi 3B, in place today. The SBC upgrade (likely an RK3588-class board) is a separate, unresolved decision. Phase 1 should be built to run correctly on the 3B now — don't block on the hardware decision, and don't write code that assumes a specific upgraded board.

---

## Definition of done for Phase 1

- **Required:** Frame Capture sprint and CAN Data sprint both complete, each producing independently timestamped, logged data.
- **Optional / stretch, pursued only after the above is done:** GPS Data sprint, IMU Data sprint.

Phase 1 is "done" the moment CAN data is flowing and logged alongside frame capture. GPS and IMU are valuable but not blocking.

---

## Component 1 — Frame Capture (first sprint)

**Intent:** capture individual camera frames at a 30 fps trigger rate, each frame written out as its own file, with every capture recorded as a row in a CSV log (filename + timestamp).

**Explicitly not this:** no video-recording library, no `cv2.VideoWriter`-style continuous video stream, no codec/container output. One frame in, one file out, every time.

**In scope:**
- A fixed-interval capture trigger targeting 30 fps
- Per-frame file output with a naming convention
- A CSV ledger: one row per captured frame, filename + timestamp

**Out of scope:**
- Any correlation logic with CAN/GPS/IMU (that's a cross-cutting concern once those streams exist — see below)
- Any frame processing, compression choices beyond what's needed to just save the image, or inference
- Cloud upload

**Camera:** IMX219 sensor.

**Worth flagging for planning:** actual achievable capture rate on the Pi 3B may fall short of 30 fps depending on storage I/O — worth measuring and documenting the real rate rather than assuming the target is hit. If this particular IMX219 unit is one of the cameras salvaged from the wound-down "AutoDrive" work with known intrinsics, confirm that and carry the calibration data forward — it matters for depth estimation in Phase 2. If it's a different unit (even the same sensor), intrinsics aren't automatically the same and it would need its own calibration.

## Component 2 — CAN Data (second sprint)

**Intent:** log CAN bus data from the Malibu with per-record timestamps, in the same spirit as frame capture — one clean, timestamped record per data point.

**Prior art to build on:** wheel speed, steering angle, yaw rate, and GPS (via OnStar) signals are already identified from earlier reverse-engineering work. CAN hardware has already been salvaged and is available. This sprint is about building the logging pipeline around known-good signals, not rediscovering them.

**In scope:**
- Reading and logging the already-identified CAN signals with timestamps
- A record format consistent enough with the frame capture CSV that the two can eventually be correlated (same timestamp convention)

**Out of scope:**
- New CAN reverse-engineering beyond what's already been done
- PSCM steering command / actuation work (that's a separate, later concern — this sprint is read-only logging)

**Open question worth resolving early:** the existing CAN work already identifies GPS via OnStar as an available signal. If that's usable, it may mean the GPS sprint doesn't need a standalone GPS module at all — worth checking during this sprint rather than waiting for the GPS sprint to find out.

## Component 3 — GPS Data (optional sprint)

**Intent:** log geolocation with timestamps, correlated to the same record convention as frame capture and CAN.

**In scope:**
- Whatever ends up being the GPS source — resolve against the OnStar-via-CAN open question above before assuming a dedicated GPS module is needed
- Timestamped logging matching the established convention

**Out of scope:**
- Anything beyond raw position logging (no mapping, no route processing)

## Component 4 — IMU Data (optional sprint)

**Intent:** log IMU data (accelerometer/gyroscope, whatever the sensor provides) with timestamps, same convention as the rest.

**In scope:**
- IMU sensor read + timestamped logging

**Out of scope:**
- Sensor fusion, filtering, or any interpretation of the IMU data — raw logged values only

---

## Cross-cutting requirements (apply to every sprint)

- Every stream gets its own timestamped record (CSV or equivalent) — consistent enough across streams that Phase 2 can align them later. Nail down the timestamp convention (precision, clock source) during the Frame Capture sprint, since every later sprint inherits it.
- No inference, no classification, no lane detection anywhere in Phase 1 — that's all Phase 2. Cloud upload happens only in the dedicated upload sprint (Sprint 6), never inside the acquisition sprints.
- Each stream should be independently runnable. CAN logging shouldn't depend on GPS being present; frame capture shouldn't depend on any of the others. This is what makes GPS/IMU safely optional.
- Build against the Pi 3B as it exists today. Don't design in a hardware dependency on the still-undecided SBC upgrade.

## Explicitly out of scope for all of Phase 1

- Any AWS-side component (processing, triggers on upload, databases). Device-side upload of finished sessions is in scope as Sprint 6
- Any on-device inference (image classification, lane detection, object detection) — previously a goal for this project, now deferred to Phase 2 under the current architecture
- The companion BLE app (start/stop recording, status check, upload trigger) — related, but a separate track of work, not a Phase 1 sprint
- Video-library-based recording of any kind
