import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.modules.setdefault("boto3", mock.Mock())  # only the clients are used, and they're mocked below
import upload_live_frame as up  # noqa: E402

from PIL import Image  # noqa: E402


def jpeg(w, h):
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (120, 90, 200)).save(buf, "JPEG", quality=90)
    return buf.getvalue()


class UploadLiveFrameTest(unittest.TestCase):
    def test_archive_key_matches_the_lambda(self):
        # same example as AWS/src/lambda/on_live_frame.py's frame_name docstring
        self.assertEqual(up.archive_key("20261020T140000Z", 1792505112.512),
                         "live/20261020T140000Z/20261020T140512.512Z.jpg")

    def test_downscale(self):
        out = Image.open(io.BytesIO(up.downscale(jpeg(1280, 1280), size=640)))
        self.assertEqual(out.size, (640, 640))
        self.assertEqual(up.downscale(b"as-is", size=0), b"as-is")

    def test_direct_mode_puts_archive_key_then_queues(self):
        s3, sqs = mock.Mock(), mock.Mock()
        key, _, _ = up.send(s3, sqs, b"jpg", "20261020T140000Z", 1792505112.512, 7, queue_url="https://q/x.fifo",
                            written_at=1792505112.7)
        put = s3.put_object.call_args.kwargs
        self.assertEqual(put["Key"], key)
        self.assertEqual(put["Metadata"]["frame-seq"], "7")
        sent = sqs.send_message.call_args.kwargs
        self.assertEqual(sent["MessageGroupId"], "20261020T140000Z")
        body = json.loads(sent["MessageBody"])
        self.assertEqual((body["key"], body["frame_time"], body["frame_seq"]), (key, 1792505112.512, 7))
        self.assertLessEqual(body["uploaded_at"], body["queued_at"])
        self.assertEqual(body["written_at"], 1792505112.7)

    def test_without_queue_url_uploads_latest_frame_for_the_lambda(self):
        s3, sqs = mock.Mock(), mock.Mock()
        key, _, send_s = up.send(s3, sqs, b"jpg", "S", 1.0, 0, queue_url=None)
        self.assertEqual((key, send_s), ("live/latest_frame.jpg", 0.0))
        sqs.send_message.assert_not_called()


if __name__ == "__main__":
    unittest.main()
