import csv
import tempfile
import unittest
from pathlib import Path

from ivision import sysmon as sm

MEMINFO = ("MemTotal:  926000 kB\nMemFree:  100000 kB\nMemAvailable:  512000 kB\nDirty:  20480 kB\n"
           "Writeback:  1024 kB\nSwapTotal:  2048000 kB\nSwapFree:  1024000 kB\n")
STAT1 = "cpu  100 0 50 800 50 0 0 0 0 0\ncpu0 1 1 1 1 1 0 0 0 0 0\n"
STAT2 = "cpu  200 0 100 900 200 0 0 0 0 0\n"   # +100 user, +50 system, +100 idle, +150 iowait
PSI1 = "some avg10=0.00 avg60=0.00 avg300=0.00 total=1000000\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=500000\n"
PSI2 = "some avg10=0.00 avg60=0.00 avg300=0.00 total=1500000\nfull avg10=0.00 avg60=0.00 avg300=0.00 total=500000\n"
VM1 = "nr_free_pages 1\nallocstall_normal 3\nallocstall_movable 1\npgscan_direct 100\npswpin 10\npswpout 20\n"
VM2 = "nr_free_pages 1\nallocstall_normal 7\nallocstall_movable 2\npgscan_direct 260\npswpin 15\npswpout 620\n"


class ParserTest(unittest.TestCase):
    def test_meminfo(self):
        m = sm.parse_meminfo(MEMINFO)
        self.assertEqual((m["MemAvailable"], m["Dirty"]), (512000, 20480))

    def test_cpu_times(self):
        self.assertEqual(sm.parse_cpu_times(STAT1), (100, 50, 50, 1000))
        self.assertIsNone(sm.parse_cpu_times("garbage"))

    def test_psi(self):
        self.assertEqual(sm.parse_psi(PSI1), {"some": 1000000, "full": 500000})
        self.assertEqual(sm.parse_psi("some avg10=0.00 avg60=0.00 avg300=0.00 total=7\n"), {"some": 7})
        self.assertEqual(sm.parse_psi(""), {})

    def test_vmstat_sums_all_allocstall_counters(self):
        self.assertEqual(sm.parse_vmstat(VM1),
                         {"allocstall": 4, "pgscan_direct": 100, "pswpin": 10, "pswpout": 20})


class FakeClock:
    def __init__(self):
        self.t = 10_000_000_000

    def __call__(self):
        return self.t


class MonitorTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.proc = self.root / "proc"
        (self.proc / "pressure").mkdir(parents=True)
        self.sys = self.root / "sys"
        (self.sys / "class/thermal/thermal_zone0").mkdir(parents=True)
        (self.sys / "devices/platform/soc/soc:firmware").mkdir(parents=True)
        (self.sys / "class/hwmon/hwmon0").mkdir(parents=True)
        (self.sys / "class/hwmon/hwmon1").mkdir(parents=True)
        (self.proc / "self").mkdir()
        self.clock = FakeClock()

    def write(self, base, name, text):
        (base / name).write_text(text)

    def monitor(self, **kw):
        path = self.root / f"system{len(list(self.root.glob('system*.csv')))}.csv"
        self.last_path = path
        return sm.SystemMonitor(path, proc_root=str(self.proc),
                                sys_root=str(self.sys), clock_ns=self.clock, **kw)

    def populate(self, stat, psi, vm):
        self.write(self.proc, "meminfo", MEMINFO)
        self.write(self.proc, "stat", stat)
        self.write(self.proc, "loadavg", "1.52 1.10 0.80 2/300 1234")
        self.write(self.proc / "pressure", "memory", psi)
        self.write(self.proc / "pressure", "io", psi)
        self.write(self.proc, "vmstat", vm)
        self.write(self.sys, "class/thermal/thermal_zone0/temp", "52600")
        self.write(self.sys, "devices/platform/soc/soc:firmware/get_throttled", "0")
        self.write(self.sys, "class/hwmon/hwmon0/name", "cpu_thermal")
        self.write(self.sys, "class/hwmon/hwmon1/name", "rpi_volt")
        self.write(self.sys, "class/hwmon/hwmon1/in0_lcrit_alarm", "1")
        self.write(self.proc, "self/status", "Name:\tpython3\nVmRSS:\t   204800 kB\n")

    def rows(self):
        with open(self.last_path, newline="") as f:
            return list(csv.DictReader(f))

    def test_first_sample_has_levels_but_blank_rates_then_rates_are_deltas(self):
        m = self.monitor(extra=lambda: {"captured": 90, "written": 88, "backlog": 2})
        self.populate(STAT1, PSI1, VM1)
        first = dict(zip(sm.COLUMNS, m.sample()))
        self.assertEqual((first["mem_available_mb"], first["dirty_mb"], first["writeback_mb"]), (500.0, 20.0, 1.0))
        self.assertEqual((first["temp_c"], first["throttled"], first["load1"]), (52.6, "0", "1.52"))
        self.assertEqual(first["cpu_user_pct"], "")
        self.assertEqual(first["backlog"], 2)

        self.clock.t += 1_000_000_000  # one second later
        self.populate(STAT2, PSI2, VM2)
        second = dict(zip(sm.COLUMNS, m.sample()))
        # cpu delta: user 100, system 50, iowait 150 of 400 ticks
        self.assertEqual((second["cpu_user_pct"], second["cpu_system_pct"], second["cpu_iowait_pct"]),
                         (25.0, 12.5, 37.5))
        # psi: some +500000 us over 1 s = 50%; full unchanged = 0%
        self.assertEqual((second["psi_mem_some_pct"], second["psi_mem_full_pct"]), (50.0, 0.0))
        self.assertEqual((second["allocstall"], second["pgscan_direct"]), (5, 160))
        # swap: pages moved during the interval, and swap in use; the recorder's RSS
        self.assertEqual((second["swapin_pages"], second["swapout_pages"]), (5, 600))
        self.assertEqual((second["swap_used_mb"], second["proc_rss_mb"]), (1000.0, 200.0))
        self.assertEqual(second["undervolt_alarm"], 1)
        m.stop()

    def test_fsync_interval_is_passed_to_the_writer(self):
        m = self.monitor(fsync_interval_s=17.0)
        self.assertEqual(m._csv.fsync_interval_s, 17.0)
        m.stop()

    def test_missing_files_leave_blanks_and_never_raise(self):
        m = self.monitor()  # nothing populated at all
        row = dict(zip(sm.COLUMNS, m.sample()))
        self.assertEqual(row["mem_available_mb"], "")
        self.assertEqual(row["temp_c"], "")
        self.assertEqual((row["undervolt_alarm"], row["swap_used_mb"], row["proc_rss_mb"]), ("", "", ""))
        self.clock.t += 1_000_000_000
        m.sample()
        m.stop()

    def test_extra_callback_errors_are_swallowed(self):
        def boom():
            raise RuntimeError("recorder gone")

        m = self.monitor(extra=boom)
        self.populate(STAT1, PSI1, VM1)
        row = dict(zip(sm.COLUMNS, m.sample()))
        self.assertEqual(row["captured"], "")
        m.stop()

    def test_thread_writes_rows_and_stop_closes_the_file(self):
        self.populate(STAT1, PSI1, VM1)
        self.last_path = self.root / "threaded.csv"
        m = sm.SystemMonitor(self.last_path, interval_s=0.05,
                             proc_root=str(self.proc), sys_root=str(self.sys))
        m.start()
        import time
        time.sleep(0.4)
        m.stop()
        rows = self.rows()
        self.assertGreaterEqual(len(rows), 4)
        self.assertEqual(list(rows[0].keys()), list(sm.COLUMNS))
        stamps = [int(r["timestamp_ns"]) for r in rows]
        self.assertEqual(stamps, sorted(stamps))

    def test_runs_against_the_real_proc_of_this_machine(self):
        m = sm.SystemMonitor(self.root / "real.csv")
        m.sample()
        row = dict(zip(sm.COLUMNS, m.sample()))
        m.stop()
        self.assertGreater(row["mem_available_mb"], 0)


if __name__ == "__main__":
    unittest.main()
