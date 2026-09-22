import tempfile
import time
import unittest
from pathlib import Path

from ivision.frames import disk_bench as db


class TmpCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name) / "bench"
        self.addCleanup(self._tmp.cleanup)


class RunVariantTest(TmpCase):
    def run_it(self, cfg, **kw):
        kw.setdefault("seconds", 0.4)
        kw.setdefault("rate", 200.0)
        kw.setdefault("size_bytes", 2048)
        return db.run_variant(self.dir, cfg, **kw)

    def test_writes_every_file_and_cleans_up(self):
        r = self.run_it(db.VARIANTS["A_current"])
        self.assertGreater(r["written"], 40)
        self.assertEqual(r["dropped"], 0)
        self.assertEqual(r["written"] + r["dropped"], r["produced"])
        self.assertFalse(self.dir.exists(), "test files must be deleted")

    def test_every_variant_runs(self):
        for name, cfg in db.VARIANTS.items():
            with self.subTest(variant=name):
                r = self.run_it(cfg, seconds=0.2)
                self.assertGreater(r["written"], 0)
                self.assertEqual(r["written"] + r["dropped"], r["produced"])

    def test_rename_variant_leaves_no_tmp_files_and_direct_variant_writes_final_name(self):
        seen = []

        def spy(path, data, rename, fsync):
            db.write_one(path, data, rename, fsync)
            seen.append((path.name, rename, sorted(p.name for p in path.parent.iterdir())))

        self.run_it(db.VARIANTS["A_current"], seconds=0.1, write=spy)
        self.assertTrue(all(not n.endswith(".tmp") for _, _, names in seen for n in names))
        seen.clear()
        self.run_it(db.VARIANTS["B_no_rename"], seconds=0.1, write=spy)
        self.assertTrue(all(rename is False for _, rename, _ in seen))

    def test_slow_disk_drops_frames_and_accounts_for_all_of_them(self):
        def slow(path, data, rename, fsync):
            time.sleep(0.05)  # 20 files/s of capacity vs 200/s offered

        r = self.run_it(db.VARIANTS["A_current"], write=slow, queue_size=8)
        self.assertGreater(r["dropped"], 0)
        self.assertGreater(r["drop_pct"], 50)
        self.assertEqual(r["written"] + r["dropped"], r["produced"])

    def test_a_single_long_write_is_reported_as_a_stall(self):
        calls = {"n": 0}

        def stalls_once(path, data, rename, fsync):
            calls["n"] += 1
            if calls["n"] == 5:
                time.sleep(0.6)

        r = self.run_it(db.VARIANTS["A_current"], write=stalls_once, seconds=0.3, queue_size=200)
        self.assertEqual(r["stalls_over_500ms"], 1)
        self.assertGreaterEqual(r["lat_ms_max"], 600)

    def test_multiple_writers_share_the_work(self):
        r = self.run_it(db.VARIANTS["D_fsync_x3"], seconds=0.2)
        self.assertEqual(r["written"] + r["dropped"], r["produced"])


class HelpersTest(unittest.TestCase):
    def test_windows_group_by_completion_time(self):
        recs = [(0.5, 0.01, 1000), (1.0, 0.02, 1000), (5.5, 0.9, 1000), (12.0, 0.01, 1000)]
        w = db.windows(recs, window_s=5.0)
        self.assertEqual([x["files"] for x in w], [2, 1, 1])
        self.assertAlmostEqual(w[1]["max_ms"], 900)
        self.assertEqual(w[0]["mb_s"], round(2000 / 5 / 1e6, 2))
        self.assertEqual(db.windows([]), [])

    MOUNTS = (
        "/dev/mmcblk0p2 / ext4 rw,noatime 0 0\n"
        "/dev/mmcblk0p1 /boot/firmware vfat rw 0 0\n"
        "/dev/sda1 /media/osfield/My\\040Drive exfat rw,nosuid 0 0\n"
        "tmpfs /run tmpfs rw 0 0\n"
    )

    def test_find_mount_picks_the_longest_matching_mountpoint(self):
        self.assertEqual(db.find_mount("/home/osfield/x", self.MOUNTS)[:3], ("/dev/mmcblk0p2", "/", "ext4"))
        self.assertEqual(db.find_mount("/boot/firmware/a", self.MOUNTS)[2], "vfat")
        usb = db.find_mount("/media/osfield/My Drive/_disk_bench", self.MOUNTS)
        self.assertEqual((usb[0], usb[2]), ("/dev/sda1", "exfat"))
        # a sibling folder that merely shares a name prefix is not on the drive
        self.assertEqual(db.find_mount("/media/osfield/My Drive2/x", self.MOUNTS)[1], "/")
        self.assertIsNone(db.find_mount("/x", "garbage line"))

    def test_base_block_device(self):
        self.assertEqual(db.base_block_device("/dev/sda1"), "sda")
        self.assertEqual(db.base_block_device("/dev/mmcblk0p2"), "mmcblk0")
        self.assertEqual(db.base_block_device("/dev/nvme0n1p3"), "nvme0n1")
        self.assertIsNone(db.base_block_device("tmpfs"))

    def test_system_info_never_raises_and_reports_free_space(self):
        lines = db.system_info(".")
        self.assertTrue(any(l.startswith("free space") for l in lines))

    def test_main_lists_and_rejects_unknown_variant(self):
        import contextlib
        import io

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(db.main(["--list"]), 0)
            self.assertEqual(db.main(["--only", "Z_nope"]), 2)
        self.assertIn("A_current", out.getvalue())

    def test_main_runs_end_to_end_small(self):
        import contextlib
        import io

        with tempfile.TemporaryDirectory() as d:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = db.main(["--dir", str(Path(d) / "b"), "--seconds", "0.2", "--rate", "100",
                                "--size-kb", "2", "--only", "A_current", "B_no_rename"])
        self.assertEqual(code, 0)
        self.assertIn("=== Summary", out.getvalue())
        self.assertIn("B_no_rename", out.getvalue())


if __name__ == "__main__":
    unittest.main()
