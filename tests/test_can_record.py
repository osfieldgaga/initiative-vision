import tempfile
import unittest
from pathlib import Path

from ivision.can import record
from ivision.can.source import FakeCanSource
from ivision.session import Session, load_manifest


class FakeArgs:
    """Just enough of a parsed argparse Namespace for record_one()."""

    def __init__(self, **kw):
        self.queue_size = 64
        self.csv_fsync_interval = 1.0
        self.session = None
        self.__dict__.update(kw)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class RecordOneTest(TmpCase):
    def test_default_creates_its_own_session(self):
        source = FakeCanSource(hz=100, num_frames=5, realtime=False, start_ns=0)
        session, summary = record.record_one(source, self.base, FakeArgs(), status_every=0)
        self.assertTrue((session.root / "can.csv").exists())
        self.assertEqual(summary["can_frames_written"], 5)

    def test_session_arg_attaches_to_an_existing_session_instead_of_creating_one(self):
        # Stands in for frames.record already having created this session.
        frame_session = Session.create(self.base)
        frame_session.set_stream("frames", status="recording")
        frame_session.flush()

        source = FakeCanSource(hz=100, num_frames=5, realtime=False, start_ns=0)
        session, summary = record.record_one(
            source, self.base, FakeArgs(session=str(frame_session.root)), status_every=0
        )

        # Same directory, not a new sibling one.
        self.assertEqual(session.root, frame_session.root)
        self.assertEqual(len(list(self.base.iterdir())), 1)
        self.assertTrue((session.root / "can.csv").exists())
        self.assertTrue((session.root / "frames").exists())  # untouched, still there

        m = load_manifest(session.root)
        self.assertEqual(m["streams"]["can"]["status"], "complete")
        self.assertEqual(m["streams"]["frames"]["status"], "recording")  # not clobbered

    def test_attaching_to_a_nonexistent_session_raises_clearly(self):
        source = FakeCanSource(hz=100, num_frames=1, realtime=False, start_ns=0)
        with self.assertRaises(OSError):
            record.record_one(
                source, self.base, FakeArgs(session=str(self.base / "no_such_session")),
                status_every=0,
            )


if __name__ == "__main__":
    unittest.main()
