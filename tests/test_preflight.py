import tempfile
import unittest
from pathlib import Path

from ivision import preflight as pf


class DecodeTest(unittest.TestCase):
    def test_throttled_bits(self):
        self.assertEqual(pf.decode_throttled(0), {"now": [], "since_boot": []})
        # 0x20000 is what the Pi showed on 2026-09-21: ARM frequency capping has occurred
        self.assertEqual(pf.decode_throttled(0x20000), {"now": [], "since_boot": ["ARM frequency capping"]})
        t = pf.decode_throttled(0x50005)
        self.assertEqual(t["now"], ["under-voltage", "throttled"])
        self.assertEqual(t["since_boot"], ["under-voltage", "throttling"])


class FakeMachine(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.proc, self.sys = self.root / "proc", self.root / "sys"
        (self.proc).mkdir()
        (self.sys / "class/thermal/thermal_zone0").mkdir(parents=True)
        (self.sys / "class/hwmon/hwmon1").mkdir(parents=True)

    def set(self, avail_mb=800, swap_used_mb=0, temp_c=50.0, alarm=0):
        (self.proc / "meminfo").write_text(
            f"MemTotal: {905 * 1024} kB\nMemAvailable: {avail_mb * 1024} kB\n"
            f"SwapTotal: {2000 * 1024} kB\nSwapFree: {(2000 - swap_used_mb) * 1024} kB\n")
        (self.sys / "class/thermal/thermal_zone0/temp").write_text(str(int(temp_c * 1000)))
        (self.sys / "class/hwmon/hwmon1/name").write_text("rpi_volt")
        (self.sys / "class/hwmon/hwmon1/in0_lcrit_alarm").write_text(str(alarm))

    def proc_entry(self, pid, name, rss_mb, cmd=""):
        d = self.proc / str(pid)
        d.mkdir(exist_ok=True)
        (d / "status").write_text(f"Name:\t{name}\nVmRSS:\t{rss_mb * 1024} kB\n")
        (d / "cmdline").write_text(cmd.replace(" ", "\0"))

    def run_it(self):
        info = pf.collect(self.proc, self.sys, disk_path=str(self.root), run_vcgencmd=False)
        return info, pf.evaluate(info)


class PreflightTest(FakeMachine):
    def test_healthy_machine_has_no_warnings(self):
        self.set()
        info, findings = self.run_it()
        self.assertEqual(findings, [])
        self.assertEqual((info["mem_available_mb"], info["swap_used_mb"], info["temp_c"]), (800, 0, 50.0))
        self.assertIn("no warnings", pf.format_findings(info, findings))

    def test_the_2026_09_21_pi_would_have_been_flagged(self):
        # 217 MB available, 678 MB swapped out, 57 C, VS Code Server using ~500 MB
        self.set(avail_mb=217, swap_used_mb=678, temp_c=57.0)
        self.proc_entry(2812, "node", 194, "/home/osfield/.vscode-server/cli/servers/Stable-x/server/node --dev")
        self.proc_entry(2955, "node", 89, "/home/osfield/.vscode-server/cli/servers/Stable-y/server/node")
        info, findings = self.run_it()
        msgs = [m for _, m in findings]
        self.assertTrue(any("217 MB of memory is available" in m and "VS Code Server" in m for m in msgs), msgs)
        self.assertTrue(any("678 MB is swapped out" in m for m in msgs), msgs)
        self.assertTrue(all(level == "WARNING" for level, _ in findings))
        self.assertEqual(info["top_memory_mb"][0][0], 194)

    def test_stale_swap_with_plenty_of_free_memory_is_only_a_note(self):
        self.set(avail_mb=540, swap_used_mb=207)
        _, findings = self.run_it()
        self.assertEqual([lvl for lvl, _ in findings], ["NOTE"])
        self.assertIn("left in swap", findings[0][1])
        self.assertNotIn("short of memory", findings[0][1])

    def test_hot_soc_and_undervoltage_alarm(self):
        self.set(temp_c=74.0, alarm=1)
        _, findings = self.run_it()
        text = " ".join(m for _, m in findings)
        self.assertIn("74.0 C", text)
        self.assertIn("under-voltage alarm", text)

    def test_low_disk_space(self):
        self.set()
        info = pf.collect(self.proc, self.sys, disk_path=str(self.root), run_vcgencmd=False)
        info["disk_free_gb"] = 2.0
        self.assertTrue(any("2.0 GB of disk" in m for _, m in pf.evaluate(info)))

    def test_since_boot_flags_are_a_note_not_a_warning(self):
        findings = pf.evaluate({"throttled": 0x20000})
        self.assertEqual(findings[0][0], "NOTE")
        self.assertIn("ARM frequency capping", findings[0][1])
        self.assertEqual(pf.evaluate({"throttled": 0x1})[0][0], "WARNING")

    def test_missing_files_give_no_findings_and_do_not_raise(self):
        info = pf.collect(self.root / "nowhere", self.root / "nowhere", disk_path=str(self.root), run_vcgencmd=False)
        self.assertEqual(pf.evaluate(info), [])

    def test_real_machine_runs(self):
        info, findings = pf.run_preflight(".")
        self.assertIn("mem_available_mb", info)
        self.assertIsInstance(findings, list)


if __name__ == "__main__":
    unittest.main()
