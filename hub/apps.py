"""
hub/apps.py — Windows 11 application control for the Remote Hub.

Stateless CLI: every invocation performs exactly one action and prints exactly
one JSON object to stdout. Exit code 0 = ok, 1 = failure (JSON still emitted).

Actions:
    list                                    visible top-level windows
    query   (--pid | --name | --title)      matching processes + their windows
    launch  --target X [--args ...]         start an exe / path / URI
            [--wait-title S] [--timeout N]
    focus   (--title | --pid | --hwnd)      bring a window to the foreground
    close   (--title | --pid | --hwnd)      graceful WM_CLOSE, then --force kill
            [--force] [--timeout N]

All waits are bounded by monotonic deadlines — nothing here can hang or spin.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict

import psutil
import win32api
import win32con
import win32gui
import win32process

try:  # package import (python -m hub.apps) or script run (python hub\apps.py)
    from hub.safety import KillSwitchEngaged, assert_alive, wait_until
except ImportError:
    from safety import KillSwitchEngaged, assert_alive, wait_until

DEFAULT_TIMEOUT = 10.0
GRACEFUL_CLOSE_GRACE = 3.0  # extra seconds a killed process gets after terminate()


# ---------------------------------------------------------------- results

def emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    """Print the single structured result and exit. The only exit path."""
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=False,
    ))
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------- windows

@dataclass
class WindowInfo:
    hwnd: int
    pid: int
    process: str
    title: str
    minimized: bool
    foreground: bool


def _window_info(hwnd: int) -> WindowInfo | None:
    try:
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            name = psutil.Process(pid).name()
        except psutil.Error:
            name = "?"
        return WindowInfo(
            hwnd=hwnd,
            pid=pid,
            process=name,
            title=win32gui.GetWindowText(hwnd),
            minimized=bool(win32gui.IsIconic(hwnd)),
            foreground=(win32gui.GetForegroundWindow() == hwnd),
        )
    except win32gui.error:
        return None


def enum_windows(title: str | None = None, pid: int | None = None,
                 process: str | None = None) -> list[WindowInfo]:
    """Visible, titled top-level windows, optionally filtered (all filters AND)."""
    found: list[WindowInfo] = []

    def cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd) or not win32gui.GetWindowText(hwnd):
            return
        info = _window_info(hwnd)
        if info is None:
            return
        if title and title.lower() not in info.title.lower():
            return
        if pid is not None and info.pid != pid:
            return
        if process and process.lower() not in info.process.lower():
            return
        found.append(info)

    win32gui.EnumWindows(cb, None)
    return found


def resolve_one_window(args) -> WindowInfo:
    """Resolve --hwnd/--pid/--title to exactly one window or emit failure."""
    if args.hwnd is not None:
        info = _window_info(args.hwnd)
        if info is None or not win32gui.IsWindow(args.hwnd):
            emit(False, args.action, error=f"no window with hwnd {args.hwnd}")
        return info

    matches = enum_windows(title=args.title, pid=args.pid)
    if not matches:
        emit(False, args.action,
             error="no window matched",
             data={"title": args.title, "pid": args.pid})
    if len(matches) > 1 and args.hwnd is None and args.pid is None:
        # Ambiguity is an error, not a guess — caller picks an hwnd and retries.
        emit(False, args.action,
             error=f"{len(matches)} windows matched; disambiguate with --hwnd",
             data=[asdict(m) for m in matches])
    return matches[0]


# ---------------------------------------------------------------- actions

def do_list(args) -> None:
    emit(True, "list", data=[asdict(w) for w in enum_windows()])


def do_query(args) -> None:
    procs = []
    for p in psutil.process_iter(["pid", "name", "exe", "status", "create_time"]):
        try:
            if args.pid is not None and p.pid != args.pid:
                continue
            if args.name and args.name.lower() not in p.info["name"].lower():
                continue
            if args.pid is None and not args.name:
                continue
            procs.append({**p.info,
                          "windows": [asdict(w) for w in enum_windows(pid=p.pid)]})
        except psutil.Error:
            continue
    if args.title:
        procs = [{"pid": None, "name": None,
                  "windows": [asdict(w) for w in enum_windows(title=args.title)]}]
    if not procs:
        emit(False, "query", error="no matching process")
    emit(True, "query", data=procs)


def do_launch(args) -> None:
    target = args.target
    exe = target if os.path.isfile(target) else shutil.which(target)

    try:
        if exe:
            proc = subprocess.Popen(
                [exe, *args.args],
                creationflags=subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP,
                close_fds=True,
            )
            launched_pid = proc.pid
        else:
            # Shell association path: URIs (ms-settings:), documents, aliases.
            # No args supported on this path by design.
            if args.args:
                emit(False, "launch",
                     error="--args requires a resolvable exe; "
                           f"{target!r} resolved via shell association only")
            os.startfile(target)
            launched_pid = None
    except (OSError, subprocess.SubprocessError) as e:
        emit(False, "launch", error=f"{type(e).__name__}: {e}")

    data = {"target": target, "resolved": exe, "launched_pid": launched_pid,
            "window": None}

    # The launched pid is often a launcher/broker (UWP, updaters). --wait-title
    # is the reliable readiness signal; pid match is best-effort sugar on top.
    if args.wait_title:
        def appeared():
            wins = enum_windows(title=args.wait_title)
            return wins[0] if wins else None

        win = wait_until(appeared, args.timeout)
        if win is None:
            emit(False, "launch", data=data,
                 error=f"launched, but no window titled ~{args.wait_title!r} "
                       f"within {args.timeout}s")
        data["window"] = asdict(win)

    emit(True, "launch", data=data)


def _force_foreground(hwnd: int) -> None:
    """SetForegroundWindow with the standard workaround for the OS focus lock."""
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    try:
        win32gui.SetForegroundWindow(hwnd)
    except win32gui.error:
        # A synthetic ALT tap lifts the foreground-lock restriction.
        win32api.keybd_event(win32con.VK_MENU, 0, 0, 0)
        win32api.keybd_event(win32con.VK_MENU, 0, win32con.KEYEVENTF_KEYUP, 0)
        win32gui.SetForegroundWindow(hwnd)


def do_focus(args) -> None:
    win = resolve_one_window(args)
    try:
        _force_foreground(win.hwnd)
    except win32gui.error as e:
        emit(False, "focus", data=asdict(win), error=f"win32 error: {e}")

    ok = wait_until(
        lambda: win32gui.GetForegroundWindow() == win.hwnd, args.timeout)
    info = _window_info(win.hwnd)
    emit(bool(ok), "focus",
         data=asdict(info) if info else asdict(win),
         error=None if ok else "window did not reach foreground in time")


def do_close(args) -> None:
    win = resolve_one_window(args)
    pid = win.pid

    win32gui.PostMessage(win.hwnd, win32con.WM_CLOSE, 0, 0)
    gone = wait_until(lambda: not win32gui.IsWindow(win.hwnd), args.timeout)

    if gone:
        emit(True, "close", data={"closed": asdict(win), "forced": False})

    if not args.force:
        emit(False, "close", data=asdict(win),
             error=f"window survived WM_CLOSE for {args.timeout}s "
                   "(unsaved-changes dialog?); rerun with --force to kill")

    try:
        p = psutil.Process(pid)
        p.terminate()
        try:
            p.wait(GRACEFUL_CLOSE_GRACE)
        except psutil.TimeoutExpired:
            p.kill()
            p.wait(GRACEFUL_CLOSE_GRACE)
    except psutil.NoSuchProcess:
        pass
    except psutil.Error as e:
        emit(False, "close", data=asdict(win), error=f"kill failed: {e}")
    emit(True, "close", data={"closed": asdict(win), "forced": True})


# ---------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hub.apps", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("list")

    q = sub.add_parser("query")
    q.add_argument("--pid", type=int)
    q.add_argument("--name")
    q.add_argument("--title")

    l = sub.add_parser("launch")
    l.add_argument("--target", required=True)
    l.add_argument("--args", nargs="*", default=[])
    l.add_argument("--wait-title")
    l.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)

    for name in ("focus", "close"):
        s = sub.add_parser(name)
        s.add_argument("--title")
        s.add_argument("--pid", type=int)
        s.add_argument("--hwnd", type=int)
        s.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
        if name == "close":
            s.add_argument("--force", action="store_true")

    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.action in ("focus", "close") and not any(
            v is not None for v in (args.title, args.pid, args.hwnd)):
        emit(False, args.action, error="need --title, --pid, or --hwnd")
    handler = {"list": do_list, "query": do_query, "launch": do_launch,
               "focus": do_focus, "close": do_close}[args.action]
    try:
        assert_alive(f"apps.{args.action}")
        handler(args)
    except KillSwitchEngaged as e:
        emit(False, args.action, error=str(e))
    except Exception as e:  # last-resort net: still emit structured JSON
        emit(False, args.action, error=f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
