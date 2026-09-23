# Initiative Vision 1 — Phase 1 Sprint Roadmap

Source: `claude/initiative-vision-1-phase1-acquisition.md`. Nothing below starts until you approve that sprint individually.

## Sprint list

| # | Sprint | Required? | Depends on | Effort (rough) |
|---|--------|-----------|------------|----------------|
| 1 | [Frame Capture + timestamp convention](sprint-1-frame-capture.md) | **Required** | — | M–L |
| 2 | [CAN logging (read-only)](sprint-2-can-logging.md) | **Required** | 1 (timestamp convention) | M–L |
| 3 | [Concurrent run + Phase 1 acceptance](sprint-3-concurrent-run-acceptance.md) | **Required** | 1, 2 | M |
| 4 | [GPS logging](sprint-4-gps-optional.md) | Optional | 2 (OnStar finding), 3 | S–M |
| 5 | [IMU logging](sprint-5-imu-optional.md) | Optional | 1 (convention), 3 | S–M |
| 6 | [AWS upload](sprint-6-aws-upload.md) | Optional, in scope (added by you) | 3 | M |

Effort is relative (S < M < L), not calendar time. I don't know your weekly hours, so I've left dates out.

The brief's definition of done (CAN flowing and logged alongside frame capture) is met at the end of Sprint 3. Sprints 4, 5 and 6 come after it, in the brief's order: GPS, then IMU, then upload. Sprint 6 doesn't need 4 or 5, so it can move earlier if you want sessions off the Pi sooner.

## Decision log

Answers you've given so far. Dated 2026-09-21.

| # | Topic | Decision |
|---|-------|----------|
| 1 | AWS upload | **Brought into Phase 1 as Sprint 6.** The brief excluded it, so I updated the brief (see "Brief changes" below). AWS-side processing stays Phase 2. The BLE app stays a separate track. |
| 2 | Optional sprints | Keep the brief's order: GPS (4), then IMU (5), then upload (6). GPS and IMU stay optional. |
| 3 | Camera | IMX219 on the CSI ribbon. **Not** an AutoDrive unit, so recorded as uncalibrated. You'll calibrate it in MATLAB later. Intrinsics only hold for the exact resolution and mode used, so the Sprint 1 operating-point choice affects when you'd need to recalibrate. |
| 4 | Storage | Internal SD card for now, SSD to be bought separately. Sprint 1 labels its numbers "SD card" and has a cheap re-run once the SSD is here. |
| 5 | Car power | Battery / power bank, so no 12V transients. Sprint 3 tests for undervoltage sag and abrupt cut-off instead. |
| 6 | Working setup | The project directory is an sshfs mount of the Pi (`osfield@192.168.0.105:/home/osfield`), so files I write land on the Pi. My shell runs on your desktop (x86_64, no camera or CAN hardware), so unit tests run there against fakes. **Hardware steps: I run them on the Pi over SSH** (`ssh osfield@192.168.0.105 '<command>'`), going through your normal permission prompts. I keep commands read-only or inside the project folder unless a step needs more, and I say so first. Not used yet: no sprint has been approved. |
| 7 | Code walkthrough | Added 2026-09-21. When Phase 1 is done you want all the code explained (what does what and how) well enough to write it yourself, because the competition is ongoing and you'd rather spend your time on AWS/OpenCV. I write a walkthrough per sprint as it closes, in `docs/walkthrough/`, splitting the essential logic from the hardening and including a minimal version you could write quickly. A final overview ties them together. I'll keep code simple and stdlib-only where I can, and comment the *why* of anything non-obvious. |
| 8 | GPS/IMU source, for now | Added 2026-09-23. Both GPS (OnStar) and the accelerometer are CAN signals on the Malibu, confirmed. You have dedicated external GPS and IMU sensors too, but are deliberately not wiring them up yet — "make it work first, then improve it": the AWS post-processing pipeline matters more right now than position/motion accuracy. So Sprints 4 and 5 reduce, near-term, to DBC decode work over Sprint 2's raw CAN log — no new hardware, no new live process — with the physical-sensor paths in each sprint doc kept ready for whenever accuracy actually becomes the bottleneck. |

### Brief changes made because of decision 1

`claude/initiative-vision-1-phase1-acquisition.md` now says device-side upload of finished sessions is in scope (Sprint 6). Three lines changed: the Phase 1 description, the cross-cutting "no cloud upload" rule, and the "out of scope" AWS bullet. The BLE app and Phase 2 processing lines are untouched.

## Still open at roadmap level

1. **Timestamp convention** is the biggest cross-sprint risk. The Pi 3B has no RTC, and the camera (libcamera) and CAN stack (SocketCAN) may use different kernel clocks. Sprint 1 settles it and needs your sign-off.
2. Sprint-level questions (CSV `seq` column, CAN hardware, signal definitions, AWS account and network, and so on) are in each sprint's "Open questions" and get asked when that sprint starts.

## Ground rules applied to every sprint

- Each stream is its own process and log, and runs without the others.
- Raw data and timestamps only: no inference, no fusion or filtering, no cloud (except the device-side upload in Sprint 6).
- Built and tested against the Pi 3B; nothing assumes the future RK3588-class board.
- Every sprint ends with a demo you can verify and a short results note, not just code.
- Each sprint file has an "Open questions" section. I'll ask those before starting, not mid-sprint.
