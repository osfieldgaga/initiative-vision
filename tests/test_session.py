import tempfile
import unittest
from pathlib import Path

from ivision.session import Session, load_manifest


class TmpDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


class SessionAttachTest(TmpDirCase):
    def test_attach_loads_the_existing_manifest(self):
        created = Session.create(self.base)
        created.set_stream("frames", status="recording")
        created.flush()

        attached = Session.attach(created.root)
        self.assertEqual(attached.root, created.root)
        self.assertEqual(attached.manifest["session_id"], created.manifest["session_id"])
        self.assertEqual(attached.manifest["streams"]["frames"]["status"], "recording")

    def test_attach_before_create_raises(self):
        with self.assertRaises(OSError):
            Session.attach(self.base / "does_not_exist")

    def test_attach_does_not_create_frames_dir(self):
        created = Session.create(self.base)
        (created.root / "frames").rmdir()  # simulate a CAN-only use of this session
        Session.attach(created.root)
        self.assertFalse((created.root / "frames").exists())


class ConcurrentFlushTest(TmpDirCase):
    """Two independent Session objects (standing in for two OS processes)
    sharing one directory -- the scenario frames.record + can.record run in
    for real. Each only ever writes its own stream key; flush() must not let
    one's stale in-memory copy of the other's data clobber it on disk."""

    def test_two_streams_set_by_different_session_objects_both_survive(self):
        a = Session.create(self.base)  # stands in for frames.record's process
        a.set_stream("frames", status="recording", frames_written=0)
        a.flush()

        b = Session.attach(a.root)  # stands in for can.record's process, attaching after
        b.set_stream("can", status="recording", can_frames_written=0)
        b.flush()

        # a's in-memory copy has no idea "can" exists yet -- if a flushes again now,
        # "can" must still survive on disk.
        a.set_stream("frames", frames_written=100)
        a.flush()

        on_disk = load_manifest(self.base / a.root.name)
        self.assertEqual(on_disk["streams"]["frames"]["frames_written"], 100)
        self.assertEqual(on_disk["streams"]["can"]["status"], "recording")
        self.assertEqual(on_disk["streams"]["can"]["can_frames_written"], 0)

    def test_finalize_from_the_second_stream_does_not_erase_the_first(self):
        a = Session.create(self.base)
        a.set_stream("frames", status="recording")
        a.flush()

        b = Session.attach(a.root)
        b.set_stream("can", status="recording")
        b.flush()

        a.set_stream("frames", status="complete")
        a.finalize()  # frames stream finishes first

        b.set_stream("can", status="complete")
        b.finalize()  # can finishes later, independently

        on_disk = load_manifest(self.base / a.root.name)
        self.assertEqual(on_disk["status"], "complete")
        self.assertEqual(on_disk["streams"]["frames"]["status"], "complete")
        self.assertEqual(on_disk["streams"]["can"]["status"], "complete")
        # stopped reflects whichever finalize() actually landed last (b's).
        self.assertEqual(on_disk["stopped"], b.manifest["stopped"])

    def test_camera_dict_from_the_creator_is_not_clobbered_by_a_stale_attacher(self):
        a = Session.create(self.base)
        a.flush()
        b = Session.attach(a.root)  # b's in-memory camera is the pre-refresh snapshot

        # The creator later learns more about the camera and refreshes.
        a.update_camera(sensor_mode=2, applied_fps=30)
        a.flush()

        # b flushes afterward for an unrelated reason (e.g. its own stream update),
        # with its stale, smaller "camera" dict still in memory.
        b.set_stream("can", status="recording")
        b.flush()

        on_disk = load_manifest(self.base / a.root.name)
        self.assertEqual(on_disk["camera"]["sensor_mode"], 2)
        self.assertEqual(on_disk["camera"]["applied_fps"], 30)


if __name__ == "__main__":
    unittest.main()
