"""System monitor: one row per second of what the machine was doing, logged
next to the video (``system.csv`` in the session folder).

Why: on 2026-09-21 the camera died mid-recording twice ("Camera frontend has
timed out") and the cause was unknown: CPU, memory, disk and power were all
suspects and nothing had been measured. With this running, a failure can be
lined up against the numbers from the same second.

Timestamps are ``time.monotonic_ns()``, the same clock as the frame ledger, so
the two files join on time. Columns that a kernel does not provide are left
blank; nothing here may ever stop a recording.

Rates are computed between samples, so the first row has blanks for them.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from .csvlog import DurableCsvWriter

COLUMNS = (
    "timestamp_ns",
    "mem_available_mb", "mem_free_mb", "dirty_mb", "writeback_mb",
    "cpu_user_pct", "cpu_system_pct", "cpu_iowait_pct", "load1",
    "temp_c", "throttled", "undervolt_alarm",
    # Pressure stall info: percent of the interval in which tasks were stalled
    # waiting for memory or I/O. "some" = at least one task, "full" = all tasks.
    "psi_mem_some_pct", "psi_mem_full_pct", "psi_io_some_pct", "psi_io_full_pct",
    # Direct reclaim: a task had to free memory itself before it could allocate.
    "allocstall", "pgscan_direct",
    # Swapping: pages moved in/out of swap during the interval, and swap in use.
    # On 2026-09-21 the Pi had about 700 MB swapped out, some of it onto the SD card
    # being recorded to. proc_rss_mb is the recorder's own memory.
    "swapin_pages", "swapout_pages", "swap_used_mb", "proc_rss_mb",
    # From the recorder, if wired up:
    "captured", "written", "backlog",
)


# -- parsers (pure functions, easy to test) ---------------------------------

def parse_meminfo(text: str) -> dict:
    """/proc/meminfo -> {name: kB}."""
    out = {}
    for line in text.splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) >= 2 and parts[1].isdigit():
            out[parts[0]] = int(parts[1])
    return out


def parse_cpu_times(text: str):
    """First 'cpu' line of /proc/stat -> (user, system, iowait, total) in ticks."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            f = [int(x) for x in line.split()[1:]]
            f += [0] * (8 - len(f))
            user, nice, system, idle, iowait, irq, softirq, steal = f[:8]
            return user + nice, system + irq + softirq, iowait, sum(f[:8])
    return None


def parse_psi(text: str) -> dict:
    """/proc/pressure/* -> {'some': total_us, 'full': total_us}."""
    out = {}
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] in ("some", "full"):
            for p in parts[1:]:
                if p.startswith("total="):
                    out[parts[0]] = int(p[6:])
    return out


def parse_vmstat(text: str) -> dict:
    """/proc/vmstat -> allocstall (sum of allocstall_*), pgscan_direct, pswpin, pswpout."""
    out = {"allocstall": 0, "pgscan_direct": 0, "pswpin": 0, "pswpout": 0}
    for line in text.splitlines():
        name, _, value = line.partition(" ")
        if name.startswith("allocstall"):
            out["allocstall"] += int(value)
        elif name in out:
            out[name] = int(value)
    return out


class SystemMonitor:
    def __init__(
        self,
        path,
        interval_s: float = 1.0,
        fsync_interval_s: float = 5.0,
        extra: Callable[[], dict] | None = None,
        proc_root: str = "/proc",
        sys_root: str = "/sys",
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ):
        self.interval_s = interval_s
        self._extra = extra
        self._proc = Path(proc_root)
        self._sys = Path(sys_root)
        self._clock_ns = clock_ns
        # A diagnostic log does not need to survive a power cut second by second, and every
        # fsync forces an ext4 journal commit that can stall the recording (see record.py).
        self._csv = DurableCsvWriter(path, COLUMNS, flush_interval_s=1.0, fsync_interval_s=fsync_interval_s)
        self._prev = None  # (timestamp_ns, cpu, psi_mem, psi_io, vmstat)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- reading -----------------------------------------------------------

    def _read(self, base: Path, name: str):
        try:
            return (base / name).read_text().strip()
        except (OSError, ValueError):
            return None

    def _undervolt_alarm(self):
        """1 if the firmware's low-voltage alarm is raised right now, else 0; blank if unknown.

        Kernel 6.18 on this Pi has no get_throttled file, but exposes the alarm through
        the rpi_volt hwmon device. (The since-boot flags are `vcgencmd get_throttled`.)
        """
        try:
            for hw in sorted((self._sys / "class/hwmon").glob("hwmon*")):
                if self._read(hw, "name") == "rpi_volt":
                    v = self._read(hw, "in0_lcrit_alarm")
                    return int(v) if v is not None and v.isdigit() else ""
        except OSError:
            pass
        return ""

    def sample(self) -> list:
        """Take one sample, write it, and return the row (used directly by tests)."""
        now = self._clock_ns()
        mem_text = self._read(self._proc, "meminfo")
        mem = parse_meminfo(mem_text) if mem_text else {}
        mb = lambda key: round(mem[key] / 1024, 1) if key in mem else ""

        cpu_text = self._read(self._proc, "stat")
        cpu = parse_cpu_times(cpu_text) if cpu_text else None
        psi = {}
        for kind in ("memory", "io"):
            t = self._read(self._proc / "pressure", kind)
            psi[kind] = parse_psi(t) if t else {}
        vm_text = self._read(self._proc, "vmstat")
        vm = parse_vmstat(vm_text) if vm_text else None

        load = self._read(self._proc, "loadavg")
        load1 = load.split()[0] if load else ""
        temp = self._read(self._sys, "class/thermal/thermal_zone0/temp")
        temp_c = round(int(temp) / 1000, 1) if temp and temp.lstrip("-").isdigit() else ""
        throttled = self._read(self._sys, "devices/platform/soc/soc:firmware/get_throttled") or ""
        swap_used = ""
        if "SwapTotal" in mem and "SwapFree" in mem:
            swap_used = round((mem["SwapTotal"] - mem["SwapFree"]) / 1024, 1)
        rss = ""
        status = self._read(self._proc, "self/status")
        if status:
            for line in status.splitlines():
                if line.startswith("VmRSS:"):
                    rss = round(int(line.split()[1]) / 1024, 1)

        cpu_pct = ["", "", ""]
        psi_pct = ["", "", "", ""]
        stalls = ["", ""]
        swaps = ["", ""]
        if self._prev is not None:
            p_now, p_cpu, p_psi, p_vm = self._prev
            elapsed_us = (now - p_now) / 1000
            if cpu and p_cpu and cpu[3] > p_cpu[3]:
                total = cpu[3] - p_cpu[3]
                cpu_pct = [round(100 * (cpu[i] - p_cpu[i]) / total, 1) for i in (0, 1, 2)]
            if elapsed_us > 0:
                for i, (kind, level) in enumerate((("memory", "some"), ("memory", "full"),
                                                   ("io", "some"), ("io", "full"))):
                    a, b = psi[kind].get(level), p_psi[kind].get(level)
                    if a is not None and b is not None:
                        psi_pct[i] = round(100 * (a - b) / elapsed_us, 1)
            if vm and p_vm:
                stalls = [vm["allocstall"] - p_vm["allocstall"], vm["pgscan_direct"] - p_vm["pgscan_direct"]]
                swaps = [vm["pswpin"] - p_vm["pswpin"], vm["pswpout"] - p_vm["pswpout"]]
        self._prev = (now, cpu, psi, vm)

        extra = {}
        if self._extra is not None:
            try:
                extra = self._extra() or {}
            except Exception:
                extra = {}
        row = [
            now, mb("MemAvailable"), mb("MemFree"), mb("Dirty"), mb("Writeback"),
            *cpu_pct, load1, temp_c, throttled, self._undervolt_alarm(), *psi_pct, *stalls,
            *swaps, swap_used, rss,
            extra.get("captured", ""), extra.get("written", ""), extra.get("backlog", ""),
        ]
        try:
            self._csv.write_row(row)
        except OSError:
            pass  # a full disk must not take the monitor (or the recording) down
        return row

    # -- running -----------------------------------------------------------

    def start(self) -> None:
        self.sample()  # baseline row, so rates start from the first interval
        self._thread = threading.Thread(target=self._run, name="sysmon", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self.sample()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        try:
            self.sample()  # final row
            self._csv.close()
        except OSError:
            pass
