# Sprint 6 — AWS Upload

**Status:** In scope (your decision, 2026-09-21). Planned, awaiting your approval to start.
**Required for Phase 1:** No. The brief's definition of done is met at Sprint 3. This is the last Phase 1 sprint.
**Depends on:** Sprint 3

## Scope decision

You chose to bring device-side upload into Phase 1. The brief had excluded it ("no cloud upload anywhere in Phase 1", and the upload trigger sits in the separate BLE-app track), so I updated the brief to match (see the roadmap's decision log). What stays out: AWS-side processing (Phase 2) and the BLE app itself. The upload here is a CLI command the BLE app can call later.

## Goal

A session directory recorded on the Pi 3B gets to S3 completely and verifiably, with no data loss and no interference with recording.

## Scope

**In:**
- Device-side upload of a completed session directory to S3
- Integrity verification, resumability, and safe local cleanup
- Credentials handling on the device

**Out (still Phase 2):**
- Anything that processes the data (OpenCV, COOL, Graviton compute, decoding, correlation)
- The AWS-side processing pipeline, triggers on upload, or databases
- The BLE app and its upload trigger. Here the upload is started by a CLI command, and the BLE app can call that later

## Decisions this sprint makes (with my recommendation)

| Decision | Recommendation | Why |
|---|---|---|
| When to upload | Only when a session is closed and the vehicle isn't recording. Never during capture | The Pi 3B's CPU, storage and USB/network bus are already saturated during recording. |
| Transport | `aws s3 sync`/`cp` or `rclone` on the finished directory first. Custom multipart code only if those prove inadequate | Reliable and resumable out of the box. Less code to get wrong. |
| Object layout | `s3://<bucket>/vehicle=<id>/session=<session_id>/...` mirroring the local layout, with `session.json` uploaded last as the "complete" marker | Phase 2 can tell a finished upload from a partial one. |
| Small files | Frames are one file each at 30 fps, about 54,000 per 30 minutes. Decide between uploading them as-is (many small PUTs, slow on a 3B) and tar-ing them into chunk archives per N frames first | This is the real cost driver. **Note:** tar chunking is packaging, not video encoding, and it stays within the brief's "one frame in, one file out" rule for capture. It only affects transport. Needs your OK. |
| Verification | Per-file or per-archive checksum computed locally and compared with S3's after upload. Delete local data only after verification, and only if you enable that | Prevents silent loss. Deleting is off by default. |
| Credentials | A least-privilege IAM identity that can write only to this prefix (no read, no list-all, no delete), stored with tight permissions on the Pi | A Pi in a car can be lost or stolen. |

## Tasks

- [ ] AWS account setup as you direct: bucket, region, encryption at rest, lifecycle rules, IAM policy. **I will not create or modify AWS resources without your explicit go-ahead at that step.**
- [ ] Measure real upload throughput from the Pi 3B on your Wi-Fi (the 3B is 2.4 GHz only) or Ethernet. Compute the time to upload a typical session. Rough scale: 30 fps at ~100–200 KB/frame is 3–6 MB/s of data being created, which is roughly 11–22 GB per hour of driving, and the upload will take a multiple of the drive time on 2.4 GHz Wi-Fi. That number decides whether the design is practical
- [ ] Upload command: `upload_session <session_dir>`, resumable and idempotent, safe to re-run
- [ ] Checksum manifest and post-upload verification
- [ ] Optional local cleanup after a verified upload, off by default
- [ ] Failure handling: network drops mid-upload, partial uploads, disk that fills while uploading
- [ ] Doc: how to run it, what lands where in S3, and the credentials setup
- [ ] Test on a real session recorded from Sprint 3, and a deliberately interrupted upload

## Acceptance criteria

1. A full session uploads to S3 with every file verified against its checksum.
2. An upload interrupted at a random point resumes and completes with no duplicates or missing files.
3. The IAM identity on the device can't read or delete existing data.
4. Recording is unaffected: uploads don't run while a session is active (enforced, not just documented).
5. The measured time to upload a typical session is documented.

## Risks

- **The bandwidth math may make upload from the Pi impractical** at the chosen frame rate and resolution. The measurement task is there to find out early. Alternatives are moving the storage medium or doing the upload from a laptop or other machine.
- **Credentials on a car-mounted device.** Mitigated with a narrow IAM policy, but never fully removed.
- **Scope creep toward Phase 2.** Anything that reads or interprets the data belongs on the AWS side.
- **Cost:** storage, requests (many small objects cost more), and data transfer. I'll show an estimate before anything is provisioned.

## Open questions for you

1. ~~Keep out / bring in / minimal?~~ Answered: bring in.
2. AWS account, region and bucket: existing or new? Who creates them?
3. How does the Pi get network access after a drive (home Wi-Fi, phone hotspot, Ethernet)?
4. Is tar-chunking of frame files for transport acceptable?
