"""
hub/power.py — power & session control for the Remote Hub.

Guarded, confirm-gated session actions: lock, monitor-off, sleep, hibernate,
shutdown, restart, plus a read-only power `status` and a shutdown `cancel`.

Design (mirrors mail's fail-closed send): every *acting* verb is a dry-run
PREVIEW by default and only takes effect with --confirm, so the orchestrator
confirms disruptive actions with the user in chat before they fire. `status`
is read-only; `cancel` (abort a pending shutdown) is an always-safe undo and
acts directly. Every effect is bounded — SendMessageTimeout for monitor power,
timeouts on shutdown.exe — so nothing here can hang the hub.

Dependency-free: ctypes against user32/powrprof/kernel32 + shutdown.exe.

CLI:
    status                                  read-only: AC/battery + active scheme
    lock            [--confirm]             LockWorkStation
    monitor-off     [--confirm]             blank the display (mouse wakes it)
    sleep           [--confirm]             suspend to RAM
    hibernate       [--confirm]             suspend to disk
    shutdown        [--confirm] [--delay N] shutdown.exe /s /t N   (default 60s)
    restart         [--confirm] [--delay N] shutdown.exe /r /t N
    cancel                                  shutdown.exe /a (abort pending)
"""

from __future__ import annotations

import argparse
import ctypes
import json
import subprocess
import sys
from ctypes import wintypes

try:  # package import or direct script run
    from hub.safety import KillSwitchEngaged, assert_alive
except ImportError:
    from safety import KillSwitchEngaged, assert_alive

DEFAULT_DELAY = 60          # seconds before a confirmed shutdown/restart fires
SUBPROC_TIMEOUT = 10.0
MONITOR_MSG_TIMEOUT = 2000  # ms; SendMessageTimeout abort-if-hung ceiling

# Human-readable plan text shown in the dry-run preview for each acting verb.
PLANS = {
    "lock": "lock the workstation (LockWorkStation)",
    "monitor-off": "turn off the display (SC_MONITORPOWER); moving the mouse wakes it",
    "sleep": "suspend the system to RAM (SetSuspendState, sleep)",
    "hibernate": "suspend the system to disk (SetSuspendState, hibernate)",
    "shutdown": "shut down Windows via shutdown.exe /s",
    "restart": "restart Windows via shutdown.exe /r",
}
ACTING = set(PLANS)


def emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=True,  # survives cp1252 consoles; parsers decode \uXXXX
    ))
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------- effects
# Small, individually-patchable helpers: each performs exactly one OS effect.
# They are only ever called on the --confirm path.

def _lock() -> None:
    if not ctypes.windll.user32.LockWorkStation():
        raise OSError("LockWorkStation returned false")


def _set_monitor_off() -> None:
    HWND_BROADCAST = 0xFFFF
    WM_SYSCOMMAND = 0x0112
    SC_MONITORPOWER = 0xF170
    SMTO_ABORTIFHUNG = 0x0002
    result = ctypes.c_ssize_t()
    rc = ctypes.windll.user32.SendMessageTimeoutW(
        HWND_BROADCAST, WM_SYSCOMMAND, SC_MONITORPOWER, 2,
        SMTO_ABORTIFHUNG, MONITOR_MSG_TIMEOUT, ctypes.byref(result))
    if rc == 0:
        raise OSError("SendMessageTimeout failed to blank the display")


def _suspend(hibernate: bool) -> None:
    # SetSuspendState(Hibernate, ForceCritical=0, DisableWakeEvent=0)
    if not ctypes.windll.powrprof.SetSuspendState(1 if hibernate else 0, 0, 0):
        raise OSError("SetSuspendState returned false")


def _shutdown(restart: bool, delay: int) -> None:
    flag = "/r" if restart else "/s"
    subprocess.run(["shutdown", flag, "/t", str(delay)], check=True,
                   capture_output=True, text=True, timeout=SUBPROC_TIMEOUT)


def _cancel_shutdown() -> tuple[bool, str]:
    """Abort a pending shutdown. Returns (something_cancelled, detail)."""
    p = subprocess.run(["shutdown", "/a"], capture_output=True, text=True,
                       timeout=SUBPROC_TIMEOUT)
    if p.returncode == 0:
        return True, "pending shutdown aborted"
    return False, "no shutdown was scheduled to cancel"


# Verb -> the effect to run on --confirm. Lambdas resolve the helper at call
# time (so tests can patch the helpers) and adapt each verb's arguments.
EFFECTS = {
    "lock": lambda a: _lock(),
    "monitor-off": lambda a: _set_monitor_off(),
    "sleep": lambda a: _suspend(False),
    "hibernate": lambda a: _suspend(True),
    "shutdown": lambda a: _shutdown(False, a.delay),
    "restart": lambda a: _shutdown(True, a.delay),
}


# ---------------------------------------------------------------- status (RO)

class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [("ACLineStatus", wintypes.BYTE),
                ("BatteryFlag", wintypes.BYTE),
                ("BatteryLifePercent", wintypes.BYTE),
                ("SystemStatusFlag", wintypes.BYTE),
                ("BatteryLifeTime", wintypes.DWORD),
                ("BatteryFullLifeTime", wintypes.DWORD)]


def _active_scheme() -> str | None:
    """Active power scheme friendly name via powercfg; fail-soft to None."""
    try:
        p = subprocess.run(["powercfg", "/getactivescheme"],
                           capture_output=True, text=True, timeout=SUBPROC_TIMEOUT)
        if p.returncode == 0 and "(" in p.stdout:
            return p.stdout.split("(", 1)[1].rsplit(")", 1)[0].strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return None


def _power_status() -> dict:
    sps = SYSTEM_POWER_STATUS()
    k = ctypes.windll.kernel32
    k.GetSystemPowerStatus.argtypes = [ctypes.POINTER(SYSTEM_POWER_STATUS)]
    k.GetSystemPowerStatus.restype = wintypes.BOOL
    ok = bool(k.GetSystemPowerStatus(ctypes.byref(sps)))
    ac_raw = sps.ACLineStatus & 0xFF if ok else 255
    pct = sps.BatteryLifePercent & 0xFF
    return {
        "ac_power": {0: "battery", 1: "ac"}.get(ac_raw, "unknown"),
        "battery_percent": None if (not ok or pct == 255) else pct,
        "active_scheme": _active_scheme(),
    }


# ---------------------------------------------------------------- dispatch

def dispatch(args) -> None:
    action = args.action

    if action == "status":
        emit(True, "status", data=_power_status())

    if action == "cancel":
        cancelled, detail = _cancel_shutdown()
        emit(True, "cancel", data={"cancelled": cancelled, "detail": detail})

    # acting verbs: dry-run preview unless --confirm
    plan = PLANS[action]
    if not args.confirm:
        data = {"dry_run": True, "confirm_required": True, "plan": plan}
        if action in ("shutdown", "restart"):
            data["delay_sec"] = args.delay
        emit(True, action, data=data)

    EFFECTS[action](args)
    data = {"confirmed": True, "plan": plan}
    if action in ("shutdown", "restart"):
        data["delay_sec"] = args.delay
        data["cancel_hint"] = "python hub\\power.py cancel"
    emit(True, action, data=data)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hub.power", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("status")
    sub.add_parser("cancel")
    for verb in ("lock", "monitor-off", "sleep", "hibernate"):
        s = sub.add_parser(verb)
        s.add_argument("--confirm", action="store_true")
    for verb in ("shutdown", "restart"):
        s = sub.add_parser(verb)
        s.add_argument("--confirm", action="store_true")
        s.add_argument("--delay", type=int, default=DEFAULT_DELAY,
                       help="seconds before it fires (default 60; gives a cancel window)")
    return p


def main() -> None:
    args = build_parser().parse_args()
    try:
        assert_alive(f"power.{args.action}")
        dispatch(args)
    except KillSwitchEngaged as e:
        emit(False, args.action, error=str(e))
    except (OSError, subprocess.SubprocessError) as e:
        emit(False, args.action, error=f"{type(e).__name__}: {e}")
    except Exception as e:
        emit(False, args.action, error=f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
