"""Live uploader: sends the recorder's latest_frame.jpg to AWS about once a second.

    LLD_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/<account-id>/lld-frames.fifo python upload_live_frame.py

Reads latest_frame.jpg, which `start.py` (record --live-preview) overwrites about twice a second.

Direct mode (LLD_QUEUE_URL set, the default way to run it): each frame is PUT straight to its
archive key live/<session-start>/<frame time>.jpg and the SQS message lld_aws.py consumes is sent
from here. This skips the S3 event -> Lambda hop, which measured ~1 s of S3 event delivery.
Without LLD_QUEUE_URL it falls back to the old path: PUT live/latest_frame.jpg and let the
on_live_frame Lambda archive and queue it.

Frames are downscaled to LIVE_SIZE (default 640x640, JPEG q70: ~25 KB instead of ~190 KB) using
the JPEG decoder's own scaling (PIL draft mode), ~40 ms on the Pi 3B. lld.py scales the camera
calibration to the frame size. LIVE_SIZE=0 uploads the file as recorded.

Times: the recorder sets the preview's mtime to the frame's *capture* time (sensor timestamp)
and its ctime is when it was written. frame-time = capture time, so the lag on EC2 starts at
the sensor; written_at = write time, so EC2 can show the capture -> write part (encode + disk
backlog) on its own. Only a fresh frame is uploaded (written less than MAX_FRAME_AGE_S ago): the
preview is rewritten every ~0.5 s, so without this the 1 upload/s gate could pick a frame that
had been sitting there for up to 0.5 s.

IAM for the Pi's credentials: s3:PutObject on arn:aws:s3:::initiative-vision-1/live/*, and in
direct mode sqs:SendMessage on the queue. Needs boto3 and Pillow in the venv.

Keep these in step with AWS/src/lambda/on_live_frame.py (frame_name, archive_key, queue_message)
-- lld_aws.py reads the same message whichever side sends it.
"""

import io
import json
import os
import time
from datetime import datetime, timezone

import boto3

BUCKET = "initiative-vision-1"
LIVE_KEY = "live/latest_frame.jpg"
LIVE_PREFIX = "live"
LOCAL_PATH = "latest_frame.jpg"
POLL_INTERVAL_S = 0.05       # how often to look for a new frame -- keeps the wait before upload short
MIN_UPLOAD_INTERVAL_S = 1.0  # at most one upload per second
MAX_FRAME_AGE_S = 0.25       # skip a frame written longer ago than this; the next one is <= 0.5 s away
LIVE_SIZE = int(os.environ.get("LIVE_SIZE", 640))  # 0 = upload as recorded
JPEG_QUALITY = 70
QUEUE_URL = os.environ.get("LLD_QUEUE_URL")  # set = direct mode


def frame_name(frame_time):
    """1792505112.512 -> '20261020T140512.512Z.jpg' (UTC, sorts chronologically as text)."""
    dt = datetime.fromtimestamp(frame_time, timezone.utc)
    return f"{dt.strftime('%Y%m%dT%H%M%S')}.{dt.microsecond // 1000:03d}Z.jpg"


def archive_key(session_start, frame_time):
    return f"{LIVE_PREFIX}/{session_start}/{frame_name(frame_time)}"


def queue_message(queue_url, body):
    """send_message kwargs; FIFO queues additionally need a group and a dedup id."""
    kwargs = {"QueueUrl": queue_url, "MessageBody": json.dumps(body)}
    if queue_url.endswith(".fifo"):
        kwargs["MessageGroupId"] = body["session_start"]  # in-order within a session
        kwargs["MessageDeduplicationId"] = body["key"][-128:]
    return kwargs


def downscale(data, size=LIVE_SIZE, quality=JPEG_QUALITY):
    """JPEG bytes -> size x size JPEG bytes. draft() makes the decoder itself output 1/2, 1/4 ...
    scale (DCT scaling), ~7x cheaper than decoding 1280x1280 and resizing."""
    if not size:
        return data
    from PIL import Image
    img = Image.open(io.BytesIO(data))
    img.draft("RGB", (size, size))
    img = img.convert("RGB")
    if img.size != (size, size):
        img = img.resize((size, size), Image.BILINEAR)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def send(s3, sqs, data, session_start, frame_time, seq, queue_url=QUEUE_URL, written_at=None):
    """Upload one frame; in direct mode also queue it. Returns (key, put_s, send_s).
    frame_time = capture time, written_at = when the recorder wrote the preview (epoch s)."""
    meta = {"session-start": session_start, "frame-time": f"{frame_time:.3f}", "frame-seq": str(seq)}
    t = time.time()
    if not queue_url:
        s3.put_object(Bucket=BUCKET, Key=LIVE_KEY, Body=data, ContentType="image/jpeg", Metadata=meta)
        return LIVE_KEY, time.time() - t, 0.0
    key = archive_key(session_start, frame_time)
    s3.put_object(Bucket=BUCKET, Key=key, Body=data, ContentType="image/jpeg", Metadata=meta)
    uploaded_at = time.time()
    body = {"bucket": BUCKET, "key": key, "session_start": session_start, "frame_time": frame_time,
            "frame_seq": seq, "uploaded_at": uploaded_at, "queued_at": time.time()}
    if written_at is not None:
        body["written_at"] = written_at
    sqs.send_message(**queue_message(queue_url, body))
    return key, uploaded_at - t, time.time() - uploaded_at


def main():
    # One session per run of this script; lld_aws.py writes results to lld/<session_start>/.
    session_start = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    s3 = boto3.client("s3")
    sqs = boto3.client("sqs") if QUEUE_URL else None
    last_written = None
    last_upload_at = 0.0
    seq = 0

    mode = f"direct -> s3://{BUCKET}/{LIVE_PREFIX}/{session_start}/ + {QUEUE_URL}" if QUEUE_URL \
        else f"-> s3://{BUCKET}/{LIVE_KEY} (Lambda queues it)"
    size = f"{LIVE_SIZE}x{LIVE_SIZE} q{JPEG_QUALITY}" if LIVE_SIZE else "as recorded"
    print(f"session {session_start}: uploading {LOCAL_PATH} {mode}, {size}", flush=True)

    while True:
        time.sleep(POLL_INTERVAL_S)
        try:
            st = os.stat(LOCAL_PATH)
        except FileNotFoundError:
            continue
        captured_at, written_at = st.st_mtime, st.st_ctime
        if written_at == last_written or time.monotonic() - last_upload_at < MIN_UPLOAD_INTERVAL_S:
            continue
        if time.time() - written_at > MAX_FRAME_AGE_S:
            continue  # stale: wait for the next write (or the recorder has stopped)
        try:
            with open(LOCAL_PATH, "rb") as f:
                raw = f.read()
        except FileNotFoundError:
            # the recorder's write is atomic (write .tmp, then rename), so this only happens if
            # the file was removed entirely -- retry next cycle
            continue

        upload_started = time.monotonic()
        t = time.perf_counter()
        try:
            data = downscale(raw)
        except Exception as e:  # e.g. a truncated file: skip this frame, not the uploader
            print(f"could not read frame, skipping: {e}", flush=True)
            last_written = written_at
            continue
        resize_s = time.perf_counter() - t
        try:
            key, put_s, send_s = send(s3, sqs, data, session_start, captured_at, seq, written_at=written_at)
        except Exception as e:
            # network drops are normal in a moving car -- keep going; last_written isn't updated,
            # so the next cycle retries with whichever fresh frame is newest by then
            print(f"upload failed, retrying: {e}", flush=True)
            continue

        last_written = written_at
        last_upload_at = upload_started
        print(f"frame {seq}: {len(data) / 1000:.0f} KB, capture->write {written_at - captured_at:.2f}s, "
              f"resize {resize_s * 1000:.0f} ms, put {put_s:.2f}s"
              + (f", send {send_s:.2f}s" if QUEUE_URL else "")
              + f", {time.time() - captured_at:.2f}s after capture", flush=True)
        seq += 1


if __name__ == "__main__":
    main()
