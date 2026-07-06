"""
hub/system.py — read-only system telemetry for the Remote Hub.

A dumb sensor by design: every subcommand samples, emits one JSON object on
stdout, and exits. No thresholds, no eval, no state changes — predicate logic
lives in the orchestrator, which compares the numeric fields this emits.

Units are explicit in key names: *_percent (0-100, whole-machine),
*_gb / *_mb (rounded), *_sec. Sampling windows are fixed and bounded.

CLI:
    snapshot [--top N] [--sample S]     everything below in one call
    cpu      [--sample S]               overall + per-core load, freq
    memory                              RAM + swap
    disk                                per-partition usage
    network                             per-NIC byte/packet counters
    battery                             present/percent/plugged or present:false
    top      [--by cpu|memory] [--count N] [--sample S]   resource hogs
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import psutil

try:  # package import or direct script run
    from hub.safety import KillSwitchEngaged, assert_alive
except ImportError:
    from safety import KillSwitchEngaged, assert_alive

DEFAULT_SAMPLE = 0.5   # seconds of CPU observation; fixed, never user-loopable
SAMPLE_MAX = 10.0      # hard cap so a bad --sample can't wedge the process
DEFAULT_TOP = 5


def _clamp_sample(sample: float) -> float:
    """Keep the CPU sampling window bounded regardless of the --sample arg."""
    return max(0.0, min(sample, SAMPLE_MAX))

_GB = 1024 ** 3
_MB = 1024 ** 2


def emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=True,  # survives cp1252 consoles; parsers decode \uXXXX
    ))
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------- sections

def sec_cpu(sample: float) -> dict:
    sample = _clamp_sample(sample)
    per_core = psutil.cpu_percent(interval=sample, percpu=True)
    freq = psutil.cpu_freq()
    return {
        "percent": round(sum(per_core) / len(per_core), 1),
        "per_core_percent": per_core,
        "cores_logical": psutil.cpu_count(logical=True),
        "cores_physical": psutil.cpu_count(logical=False),
        "freq_mhz": round(freq.current) if freq else None,
        "sample_sec": sample,
    }


def sec_memory() -> dict:
    vm, sw = psutil.virtual_memory(), psutil.swap_memory()
    return {
        "total_gb": round(vm.total / _GB, 2),
        "used_gb": round(vm.used / _GB, 2),
        "available_gb": round(vm.available / _GB, 2),
        "percent": vm.percent,
        "swap_total_gb": round(sw.total / _GB, 2),
        "swap_percent": sw.percent,
    }


def sec_disk() -> list[dict]:
    out = []
    for part in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(part.mountpoint)
        except OSError:
            continue  # unready device (empty card reader / DVD)
        out.append({
            "mount": part.mountpoint,
            "fstype": part.fstype,
            "total_gb": round(usage.total / _GB, 2),
            "free_gb": round(usage.free / _GB, 2),
            "percent": usage.percent,
        })
    return out


def sec_network() -> dict:
    per_nic = psutil.net_io_counters(pernic=True)
    total = psutil.net_io_counters()
    return {
        "total": {"sent_mb": round(total.bytes_sent / _MB, 1),
                  "recv_mb": round(total.bytes_recv / _MB, 1)},
        "nics": {
            name: {"sent_mb": round(io.bytes_sent / _MB, 1),
                   "recv_mb": round(io.bytes_recv / _MB, 1),
                   "errors": io.errin + io.errout,
                   "drops": io.dropin + io.dropout}
            for name, io in per_nic.items()
        },
    }


def sec_battery() -> dict:
    batt = psutil.sensors_battery()
    if batt is None:
        return {"present": False}
    return {
        "present": True,
        "percent": round(batt.percent, 1),
        "plugged_in": batt.power_plugged,
        "secs_left": None if batt.secsleft in (
            psutil.POWER_TIME_UNLIMITED, psutil.POWER_TIME_UNKNOWN
        ) else batt.secsleft,
    }


def sec_uptime() -> dict:
    boot = psutil.boot_time()
    return {"boot_epoch": boot, "uptime_sec": round(time.time() - boot)}


def sec_top(by: str, count: int, sample: float) -> list[dict]:
    """Top processes by whole-machine CPU percent or RSS memory.

    CPU needs two observations: prime every process counter, wait the fixed
    sample window, then read the delta.
    """
    sample = _clamp_sample(sample)
    cores = psutil.cpu_count(logical=True) or 1
    procs = []
    for p in psutil.process_iter(["pid", "name"]):
        if p.pid == 0:
            continue  # System Idle Process measures idleness, not load
        try:
            p.cpu_percent(None)  # prime
            procs.append(p)
        except psutil.Error:
            continue
    time.sleep(sample)

    rows = []
    for p in procs:
        try:
            rows.append({
                "pid": p.pid,
                "name": p.info["name"],
                # psutil reports per-core basis; normalize to whole machine
                "cpu_percent": round(p.cpu_percent(None) / cores, 1),
                "memory_mb": round(p.memory_info().rss / _MB, 1),
            })
        except psutil.Error:
            continue  # process died between passes

    key = "cpu_percent" if by == "cpu" else "memory_mb"
    rows.sort(key=lambda r: r[key], reverse=True)
    return rows[:count]


def sec_snapshot(top_n: int, sample: float) -> dict:
    return {
        "uptime": sec_uptime(),
        "cpu": sec_cpu(sample),
        "memory": sec_memory(),
        "disk": sec_disk(),
        "network": sec_network(),
        "battery": sec_battery(),
        "top_cpu": sec_top("cpu", top_n, sample),
        "top_memory": sec_top("memory", top_n, 0),  # RSS needs no window
    }


# ---------------------------------------------------------------- cli

def main() -> None:
    p = argparse.ArgumentParser(prog="hub.system", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    s = sub.add_parser("snapshot")
    s.add_argument("--top", type=int, default=DEFAULT_TOP)
    s.add_argument("--sample", type=float, default=DEFAULT_SAMPLE)

    c = sub.add_parser("cpu")
    c.add_argument("--sample", type=float, default=DEFAULT_SAMPLE)

    sub.add_parser("memory")
    sub.add_parser("disk")
    sub.add_parser("network")
    sub.add_parser("battery")

    t = sub.add_parser("top")
    t.add_argument("--by", choices=["cpu", "memory"], default="cpu")
    t.add_argument("--count", type=int, default=DEFAULT_TOP)
    t.add_argument("--sample", type=float, default=DEFAULT_SAMPLE)

    args = p.parse_args()
    try:
        assert_alive(f"system.{args.action}")
        data = {
            "snapshot": lambda: sec_snapshot(args.top, args.sample),
            "cpu": lambda: sec_cpu(args.sample),
            "memory": sec_memory,
            "disk": sec_disk,
            "network": sec_network,
            "battery": sec_battery,
            "top": lambda: sec_top(args.by, args.count, args.sample),
        }[args.action]()
        emit(True, args.action, data=data)
    except KillSwitchEngaged as e:
        emit(False, args.action, error=str(e))
    except Exception as e:
        emit(False, args.action, error=f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
