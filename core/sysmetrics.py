"""Container-correct system metrics.

psutil reads /proc/stat, /proc/meminfo and the host boot time — all of which
are HOST-global, so inside a container (the HF Space) it reports the machine
around us instead of our own cgroup. This module prefers cgroup-v2 (then v1)
accounting — our cgroup IS our machine — and falls back to psutil only where
cgroup data doesn't exist (bare metal, macOS, Windows).

API is psutil-shaped so callers swap one line:
    cpu_percent(interval)  -> float 0..100 (psutil semantics: first
                               interval=None call primes and returns 0.0)
    memory_stats()         -> {"percent", "used", "total"} in bytes
    cpu_temperature()      -> °C, or -1.0 when unknowable (always in a container)
    uptime_seconds()       -> seconds since THIS container/box started
    process_count()        -> PIDs visible in our namespace
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import psutil

_CG_V2 = Path("/sys/fs/cgroup")
_CG_V1_CPU = Path("/sys/fs/cgroup/cpuacct")
_CG_V1_MEM = Path("/sys/fs/cgroup/memory")

_container: bool | None = None
_cpu_last: tuple[float, int] | None = None   # (monotonic, usage_usec or ns-as-µs)
_uptime_start: float | None = None


# ── environment ──────────────────────────────────────────────────────────────

def _read(path: Path) -> str | None:
    try:
        return path.read_text().strip()
    except Exception:
        return None


def in_container() -> bool:
    """True when we're inside a container (HF Space, Docker, k8s, podman)."""
    global _container
    if _container is not None:
        return _container
    signals = (
        Path("/.dockerenv").exists(),
        Path("/run/.containerenv").exists(),
        bool(os.environ.get("KUBERNETES_SERVICE_HOST")),
    )
    if not any(signals):
        cg = _read(Path("/proc/self/cgroup")) or ""
        hit = any(s in cg for s in ("docker", "kubepods", "containerd",
                                    "libpod", "buildkit", "lxc"))
        if not hit:
            # Overlayfs root is what container runtimes mount; a bare-metal
            # root is ext4/xfs/btrfs. Bias toward "container" — a false
            # positive merely picks the cgroup view of the same machine.
            try:
                mi = Path("/proc/self/mountinfo").read_text()
                root_line = next((l for l in mi.splitlines()
                                  if l.split()[4] == "/"), "")
                hit = " - overlay " in f" {root_line} "
            except Exception:
                hit = False
        signals = (hit,)
    _container = any(signals)
    return _container


# ── cpu ──────────────────────────────────────────────────────────────────────

def _cpu_width() -> int:
    """CPUs this cgroup may use: quota/period if finite, else cpuset, else nproc."""
    raw = _read(_CG_V2 / "cpu.max")
    if raw:
        parts = raw.split()
        if len(parts) == 2 and parts[0] != "max":
            try:
                quota, period = int(parts[0]), int(parts[1])
                if period > 0 and quota > 0:
                    return max(1, round(quota / period))
            except ValueError:
                pass
    for p in (_CG_V2 / "cpuset.cpus.effective", _CG_V2 / "cpuset/cpuset.cpus"):
        raw = _read(p)
        if raw:
            try:
                n = sum((b - a + 1) if "-" in seg else 1
                        for seg in raw.replace(" ", "").split(",")
                        for a, b in ((int(seg.split("-")[0]),
                                      int(seg.split("-")[-1])),))
                if n > 0:
                    return n
            except ValueError:
                pass
    return os.cpu_count() or 1


def _cgroup_cpu_usage_usec() -> int | None:
    """Cumulative CPU time this cgroup has burned, in microseconds."""
    raw = _read(_CG_V2 / "cpu.stat")
    if raw:
        for line in raw.splitlines():
            if line.startswith("usage_usec "):
                try:
                    return int(line.split()[1])
                except (IndexError, ValueError):
                    return None
    raw = _read(_CG_V1_CPU / "cpuacct.usage")   # v1: nanoseconds
    if raw:
        try:
            return int(raw) // 1000
        except ValueError:
            return None
    return None


def cpu_percent(interval: float | None = None) -> float:
    """CPU use in % (0..100 = full quota), psutil call semantics."""
    global _cpu_last

    usage = _cgroup_cpu_usage_usec() if in_container() else None
    if usage is None:
        return float(psutil.cpu_percent(interval=interval))

    now = time.monotonic()
    if interval:
        # Measure exactly over this window (psutil returns a real value on
        # its first interval call too — no prime step here).
        t0, u0 = time.monotonic(), usage
        time.sleep(interval)
        now = time.monotonic()
        after = _cgroup_cpu_usage_usec()
        if after is None:
            return 0.0
        d_wall = max(now - t0, 1e-6) * 1_000_000
        pct = (after - u0) / d_wall * 100.0 / _cpu_width()
        _cpu_last = (now, after)
        return max(0.0, min(100.0, pct))

    if _cpu_last is None:
        _cpu_last = (now, usage)          # prime (psutil returns 0.0 here too)
        return 0.0
    d_wall = max(now - _cpu_last[0], 1e-6) * 1_000_000
    pct = (usage - _cpu_last[1]) / d_wall * 100.0 / _cpu_width()
    _cpu_last = (now, usage)
    return max(0.0, min(100.0, pct))


# ── memory ───────────────────────────────────────────────────────────────────

def _v1_mem() -> tuple[int, int, int] | None:
    """(used_bytes, total_bytes, inactive_file) from cgroup v1, else None."""
    cur = _read(_CG_V1_MEM / "memory.usage_in_bytes")
    if not cur:
        return None
    try:
        used = int(cur)
    except ValueError:
        return None
    inactive = 0
    stat = _read(_CG_V1_MEM / "memory.stat") or ""
    for line in stat.splitlines():
        if line.startswith("total_inactive_file "):
            try:
                inactive = int(line.split()[1])
            except (IndexError, ValueError):
                pass
            break
    total = None
    raw = _read(_CG_V1_MEM / "memory.limit_in_bytes")
    if raw:
        try:
            limit = int(raw)
            # v1 uses a huge sentinel for "unlimited"
            if limit < (1 << 60):
                total = limit
        except ValueError:
            pass
    return used, total, inactive


def memory_stats() -> dict:
    """{"percent", "used", "total"} — our cgroup when in a container, else host.

    Page cache (inactive_file) is subtracted: it's reclaimable and is what
    makes naive container readings look permanently near-full.
    """
    percent = used = total = None
    if in_container():
        cur = _read(_CG_V2 / "memory.current")
        if cur:
            try:
                used = int(cur)
            except ValueError:
                used = None
            if used is not None:
                inactive = 0
                stat = _read(_CG_V2 / "memory.stat") or ""
                for line in stat.splitlines():
                    if line.startswith("inactive_file "):
                        try:
                            inactive = int(line.split()[1])
                        except (IndexError, ValueError):
                            pass
                        break
                used = max(used - inactive, 0)
            raw = _read(_CG_V2 / "memory.max")
            if raw and raw != "max":
                try:
                    total = int(raw)
                except ValueError:
                    total = None
        else:
            v1 = _v1_mem()
            if v1:
                u, t, inactive = v1
                used, total = max(u - inactive, 0), t

    if total is None or used is None:
        vm = psutil.virtual_memory()
        percent = float(vm.percent)
        used = used if used is not None else int(vm.used)
        total = int(vm.total)
    else:
        percent = (used / total * 100.0) if total else 0.0
    return {"percent": round(percent, 1), "used": int(used), "total": int(total)}


# ── temperature / uptime / processes ────────────────────────────────────────

def cpu_temperature() -> float:
    """°C, or -1.0 when unknowable — always -1 inside a container (host sensors)."""
    if in_container():
        return -1.0
    try:
        temps = psutil.sensors_temperatures()
        for name in ("coretemp", "k10temp", "cpu_thermal", "acpitz",
                     "cpu-thermal", "zenpower", "it8688"):
            if name in temps and temps[name]:
                return float(temps[name][0].current)
        for entries in temps.values():
            if entries:
                return float(entries[0].current)
    except Exception:
        pass
    if os.name == "nt":                       # Windows: WMI, zero subprocess
        try:
            import wmi  # type: ignore
            tz = wmi.WMI(namespace="root/wmi").MSAcpi_ThermalZoneTemperature()
            if tz:
                return (tz[0].CurrentTemperature / 10.0) - 273.15
        except Exception:
            pass
    return -1.0


def _pid1_start_time() -> float | None:
    """Host-clock time when PID 1 was spawned — i.e. container start.

    /proc/1/stat's starttime is ticks since HOST boot; PID 1 in a container
    is created when the container starts. host_boot + ticks/CLK_TCK = then.
    """
    try:
        s = Path("/proc/1/stat").read_text()
        rest = s[s.rindex(")") + 2:].split()
        start_ticks = int(rest[19])                  # field 22 (starttime)
        hz = float(os.sysconf("SC_CLK_TCK"))
        return float(psutil.boot_time()) + start_ticks / hz
    except Exception:
        return None


def uptime_seconds() -> float:
    """Seconds since this box/container started.

    On the host: standard boot time. In a container the host boot time is
    wrong, so derive container start from PID 1, then /.dockerenv's ctime,
    then (last resort) our own process start — anything but host uptime.
    """
    global _uptime_start
    if not in_container():
        try:
            return max(0.0, time.time() - float(psutil.boot_time()))
        except Exception:
            pass
    if _uptime_start is None:
        _uptime_start = _pid1_start_time()
        if _uptime_start is None:
            for p in (Path("/.dockerenv"), Path("/run/.containerenv")):
                try:
                    _uptime_start = p.stat().st_ctime
                    break
                except Exception:
                    continue
        if _uptime_start is None:
            try:
                _uptime_start = psutil.Process().create_time()
            except Exception:
                _uptime_start = time.time()
    return max(0.0, time.time() - _uptime_start)


def process_count() -> int:
    try:
        return len(psutil.pids())
    except Exception:
        return 0
