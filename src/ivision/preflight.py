"""Preflight: look at the machine before recording and say what will likely go wrong.

Why: on 2026-09-21 a recording failed six different ways over several hours (disk
stalls, camera "frontend timed out", a hang). The causes turned out to be the
machine, not the code: VS Code Server and a desktop were using most of the Pi's
905 MB (only ~200 MB free, ~700 MB swapped out, partly onto the SD card being
recorded to), and the SoC reached 81 C in two minutes with no heatsink. All of
that was visible before the first frame. Warnings never block a recording; they
are printed and saved in the session manifest.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

MIN_AVAILABLE_MB = 400      # below this the disk write cushion shrinks and reclaim stalls appear
MAX_SWAP_USED_MB = 100
MAX_START_TEMP_C = 70
MIN_FREE_DISK_GB = 5


def decode_throttled(value: int) -> dict:
    """Split `vcgencmd get_throttled` into what is true now and what has happened since boot."""
    now = {0: "under-voltage", 1: "ARM frequency capped", 2: "throttled", 3: "soft temperature limit"}
    since = {16: "under-voltage", 17: "ARM frequency capping", 18: "throttling", 19: "soft temperature limit"}
    return {
        "now": [name for bit, name in now.items() if value >> bit & 1],
        "since_boot": [name for bit, name in since.items() if value >> bit & 1],
    }


def _read(path: Path):
    try:
        return path.read_text().strip()
    except (OSError, ValueError):
        return None


def _top_memory_users(proc: Path, n: int = 4) -> list:
    """[(rss_mb, description)] for the biggest processes, from /proc/<pid>/status."""
    users = []
    try:
        pids = [p for p in proc.iterdir() if p.name.isdigit()]
    except OSError:
        return []
    for p in pids:
        status = _read(p / "status")
        if not status:
            continue
        name, rss = "?", 0
        for line in status.splitlines():
            if line.startswith("Name:"):
                name = line.split(None, 1)[1]
            elif line.startswith("VmRSS:"):
                rss = int(line.split()[1])
        cmd = (_read(p / "cmdline") or "").replace("\0", " ").strip()
        users.append((rss / 1024, (cmd or name)[:110]))
    return sorted(users, reverse=True)[:n]


def collect(proc_root="/proc", sys_root="/sys", disk_path=".", run_vcgencmd=True) -> dict:
    proc, sysr = Path(proc_root), Path(sys_root)
    info = {}
    mem = {}
    for line in (_read(proc / "meminfo") or "").splitlines():
        parts = line.replace(":", " ").split()
        if len(parts) >= 2 and parts[1].isdigit():
            mem[parts[0]] = int(parts[1])
    if "MemAvailable" in mem:
        info["mem_available_mb"] = round(mem["MemAvailable"] / 1024)
        info["mem_total_mb"] = round(mem.get("MemTotal", 0) / 1024)
    if "SwapTotal" in mem and "SwapFree" in mem:
        info["swap_used_mb"] = round((mem["SwapTotal"] - mem["SwapFree"]) / 1024)
    temp = _read(sysr / "class/thermal/thermal_zone0/temp")
    if temp and temp.lstrip("-").isdigit():
        info["temp_c"] = round(int(temp) / 1000, 1)
    try:
        for hw in sorted((sysr / "class/hwmon").glob("hwmon*")):
            if _read(hw / "name") == "rpi_volt":
                v = _read(hw / "in0_lcrit_alarm")
                if v is not None and v.isdigit():
                    info["undervolt_alarm"] = int(v)
    except OSError:
        pass
    if run_vcgencmd:
        try:
            out = subprocess.run(["vcgencmd", "get_throttled"], capture_output=True, text=True, timeout=3).stdout
            if "0x" in out:
                info["throttled"] = int(out.split("0x")[1].split()[0], 16)
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
    try:
        info["disk_free_gb"] = round(shutil.disk_usage(disk_path).free / 1e9, 1)
    except OSError:
        pass
    info["top_memory_mb"] = [(round(mb), desc) for mb, desc in _top_memory_users(proc)]
    return info


def evaluate(info: dict) -> list:
    """Turn collected facts into (level, message) findings. Levels: WARNING, NOTE."""
    out = []
    avail = info.get("mem_available_mb")
    if avail is not None and avail < MIN_AVAILABLE_MB:
        msg = (f"only {avail} MB of memory is available (of {info.get('mem_total_mb')} MB); "
               f"recording is safest with {MIN_AVAILABLE_MB}+ MB. Biggest users: "
               + "; ".join(f"{mb} MB {desc[:60]}" for mb, desc in info.get("top_memory_mb", [])[:3]))
        vscode = [mb for mb, desc in info.get("top_memory_mb", []) if "vscode-server" in desc]
        if vscode or any("vscode-server" in d for _, d in info.get("top_memory_mb", [])):
            msg += ". VS Code Server is running on this machine: disconnect VS Code from the Pi"
        out.append(("WARNING", msg))
    swap = info.get("swap_used_mb")
    if swap is not None and swap > MAX_SWAP_USED_MB:
        if avail is not None and avail >= MIN_AVAILABLE_MB:
            out.append(("NOTE", f"{swap} MB is left in swap from earlier memory pressure. Memory is fine now, "
                                f"but touching those pages reads the SD card; `sudo swapoff -a && sudo swapon -a` "
                                f"clears it (needs the free RAM)"))
        else:
            out.append(("WARNING", f"{swap} MB is swapped out: the machine is short of memory, and swap "
                                   f"on the SD card competes with the recording for the same disk"))
    temp = info.get("temp_c")
    if temp is not None and temp > MAX_START_TEMP_C:
        out.append(("WARNING", f"the SoC is already {temp} C. On 2026-09-21 an open-air Pi 3B went from "
                               f"59 C to 81 C in two minutes of recording; let it cool, and fit a heatsink or fan"))
    if info.get("undervolt_alarm") == 1:
        out.append(("WARNING", "the firmware's under-voltage alarm is raised right now: check the power supply/bank"))
    if "throttled" in info:
        t = decode_throttled(info["throttled"])
        if t["now"]:
            out.append(("WARNING", "right now: " + ", ".join(t["now"]) + f" (get_throttled=0x{info['throttled']:x})"))
        if t["since_boot"]:
            out.append(("NOTE", "since boot: " + ", ".join(t["since_boot"]) + f" (get_throttled=0x{info['throttled']:x})"))
    free = info.get("disk_free_gb")
    if free is not None and free < MIN_FREE_DISK_GB:
        out.append(("WARNING", f"only {free} GB of disk space is free where sessions are written"))
    return out


def run_preflight(disk_path=".", **kw):
    info = collect(disk_path=disk_path, **kw)
    return info, evaluate(info)


def format_findings(info: dict, findings: list) -> str:
    facts = ", ".join(f"{k}={v}" for k, v in info.items() if k != "top_memory_mb")
    lines = [f"preflight: {facts}"]
    lines += [f"preflight {lvl}: {msg}" for lvl, msg in findings]
    if not any(l == "WARNING" for l, _ in findings):
        lines.append("preflight: no warnings")
    return "\n".join(lines)
