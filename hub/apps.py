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

# `kill --name` refuses these outright — terminating any of them by name would
# destabilise Windows or kill the hub/dev env. Belt-and-suspenders on top of
# the current-user-only filter (which already excludes system/other-user procs).
CRITICAL_KILL_GUARD = {
    "system", "system idle process", "registry", "memcompression",
    "smss.exe", "csrss.exe", "wininit.exe", "winlogon.exe", "services.exe",
    "lsass.exe", "lsaiso.exe", "svchost.exe", "dwm.exe", "fontdrvhost.exe",
    "spoolsv.exe", "audiodg.exe", "ctfmon.exe", "sihost.exe", "taskhostw.exe",
    "runtimebroker.exe", "wudfhost.exe", "conhost.exe", "explorer.exe",
    "textinputhost.exe", "searchhost.exe", "searchapp.exe",
    "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "applicationframehost.exe", "lockapp.exe",
    "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe",
    "openconsole.exe", "python.exe", "py.exe", "pythonw.exe",
    "wscript.exe", "cscript.exe", "ssh-agent.exe",
    "code.exe", "code - insiders.exe", "cursor.exe", "node.exe",
    "claude.exe", "taskmgr.exe",
}


# ---------------------------------------------------------------- results

def emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    """Print the single structured result and exit. The only exit path."""
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=True,  # survives cp1252 consoles; parsers decode \uXXXX
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


# Friendly names so `open spotify` works without knowing the exe/URI. Unknown
# names fall through to launching the name as-is (resolved on PATH / by the OS).
APP_ALIASES = {
    "spotify": "spotify:", "chrome": "chrome.exe", "edge": "msedge.exe",
    "firefox": "firefox.exe", "discord": "discord.exe", "steam": "steam.exe",
    "epic": "com.epicgames.launcher:", "notepad": "notepad.exe",
    "calculator": "calc.exe", "calc": "calc.exe", "paint": "mspaint.exe",
    "settings": "ms-settings:", "explorer": "explorer.exe",
    "files": "explorer.exe", "terminal": "wt.exe", "code": "code",
    "vscode": "code", "camera": "microsoft.windows.camera:",
    "photos": "ms-photos:", "taskmgr": "taskmgr.exe",
    "task-manager": "taskmgr.exe",
}


def _launch_target(target: str, extra_args=()) -> tuple[str | None, int | None]:
    """Launch an exe (resolved on PATH) or a shell association (URI/alias).
    Returns (resolved_exe_or_None, launched_pid_or_None). Raises ValueError if
    args are given for a non-exe target; OSError on launch failure."""
    exe = target if os.path.isfile(target) else shutil.which(target)
    if exe:
        proc = subprocess.Popen(
            [exe, *extra_args],
            creationflags=subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP,
            close_fds=True)
        return exe, proc.pid
    if extra_args:
        raise ValueError("--args requires a resolvable exe; "
                         f"{target!r} resolves via shell association only")
    os.startfile(target)   # URIs (ms-settings:), documents, app aliases
    return None, None


def do_open(args) -> None:
    """Friendly open: `open spotify` / `open notepad`. Resolves a known alias,
    else launches the given name directly."""
    app = args.name
    target = APP_ALIASES.get(app.strip().lower(), app)
    try:
        exe, launched_pid = _launch_target(target)
    except (OSError, subprocess.SubprocessError) as e:
        emit(False, "open", data={"app": app, "target": target},
             error=f"could not open {app!r}: {type(e).__name__}: {e}")
    emit(True, "open", data={"app": app, "target": target, "resolved": exe,
                             "launched_pid": launched_pid})


def do_launch(args) -> None:
    target = args.target
    try:
        exe, launched_pid = _launch_target(target, args.args)
    except ValueError as e:
        emit(False, "launch", error=str(e))
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


def _close_by_name(name: str, timeout: float) -> None:
    """Friendly close: gracefully close every visible window whose process
    matches `name` (e.g. `close spotify`). Graceful WM_CLOSE only — a window
    with unsaved changes survives and is reported kept, never force-killed."""
    q = name.strip().lower()
    matches = [w for w in enum_windows() if q in (w.process or "").lower()]
    if not matches:
        emit(False, "close", data={"name": name},
             error=f"no open window for {name!r} "
                   f"(a tray-only app may need `apps kill --name {q}.exe`)")
    results = []
    for w in matches:
        win32gui.PostMessage(w.hwnd, win32con.WM_CLOSE, 0, 0)
        gone = wait_until(lambda h=w.hwnd: not win32gui.IsWindow(h), timeout)
        results.append({"hwnd": w.hwnd, "process": w.process,
                        "title": w.title, "closed": bool(gone)})
    ok = all(r["closed"] for r in results)
    emit(ok, "close", data={"name": name, "windows": results, "forced": False},
         error=None if ok else "one or more windows survived (unsaved changes?); "
                               "nothing was force-killed")


def do_close(args) -> None:
    if getattr(args, "name", None):          # friendly: `close <app-name>`
        _close_by_name(args.name, args.timeout)
    win = resolve_one_window(args)            # precise: --title/--pid/--hwnd
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


def do_kill(args) -> None:
    """Terminate current-user processes by exact name, graceful-first.

    Fail-CLOSED owner scoping: (1) refuses names on CRITICAL_KILL_GUARD;
    (2) refuses entirely if the invoking user can't be resolved; (3) targets a
    process ONLY if its owner is positively readable AND equals the invoking
    user — a process whose owner is None/unreadable (every elevated/SYSTEM
    process on Windows) is skipped, never killed; (4) tries WM_CLOSE before
    terminate(). Windowless tray apps have no window to close, so they fall
    straight through to a clean TerminateProcess (no SIGTERM on Windows).
    """
    name = args.name
    if name.lower() in CRITICAL_KILL_GUARD:
        emit(False, "kill", data={"name": name},
             error=f"refused: {name!r} is a protected/critical process")

    try:
        me = psutil.Process().username()
    except psutil.Error:
        me = None
    if not me:  # can't establish identity -> fail closed, kill nothing
        emit(False, "kill", data={"name": name},
             error="cannot resolve invoking user; refusing kill (fail-closed)")

    targets = []
    for p in psutil.process_iter(["pid", "name", "username"]):
        try:
            if (p.info["name"] or "").lower() != name.lower():
                continue
            # positive same-user match only: unreadable/other owner -> skip
            if p.info.get("username") != me:
                continue
            targets.append(p)
        except psutil.Error:
            continue

    if args.dry_run:
        emit(True, "kill", data={"name": name, "matched": len(targets),
                                 "dry_run": True, "pids": [p.pid for p in targets]})
    if not targets:
        emit(True, "kill", data={"name": name, "matched": 0, "terminated": []})

    results = []
    for p in targets:
        pid = p.pid
        try:
            posted = False
            for w in enum_windows(pid=pid):          # graceful first
                win32gui.PostMessage(w.hwnd, win32con.WM_CLOSE, 0, 0)
                posted = True
            # only wait out the deadline if something was actually asked to close
            if posted and wait_until(lambda: not psutil.pid_exists(pid), args.timeout):
                results.append({"pid": pid, "method": "graceful", "terminated": True})
                continue
            p.terminate()
            try:
                p.wait(GRACEFUL_CLOSE_GRACE)
            except psutil.TimeoutExpired:
                p.kill()
                p.wait(GRACEFUL_CLOSE_GRACE)
            results.append({"pid": pid, "method": "terminate", "terminated": True})
        except psutil.NoSuchProcess:
            results.append({"pid": pid, "method": "already-exited", "terminated": True})
        except psutil.Error as e:
            results.append({"pid": pid, "terminated": False, "error": str(e)})

    ok = all(r.get("terminated") for r in results)
    emit(ok, "kill",
         data={"name": name, "matched": len(targets), "terminated": results},
         error=None if ok else "one or more processes survived")


# ---------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="hub.apps", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("list")

    q = sub.add_parser("query")
    q.add_argument("--pid", type=int)
    q.add_argument("--name")
    q.add_argument("--title")

    o = sub.add_parser("open")
    o.add_argument("name", help="app to open, e.g. spotify | chrome | notepad")

    l = sub.add_parser("launch")
    l.add_argument("--target", required=True)
    l.add_argument("--args", nargs="*", default=[])
    l.add_argument("--wait-title")
    l.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)

    for verb in ("focus", "close"):
        s = sub.add_parser(verb)
        s.add_argument("--title")
        s.add_argument("--pid", type=int)
        s.add_argument("--hwnd", type=int)
        s.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
        if verb == "close":
            s.add_argument("name", nargs="?",
                           help="app to close, e.g. spotify (or use --title/--hwnd)")
            s.add_argument("--force", action="store_true")

    k = sub.add_parser("kill")
    k.add_argument("--name", required=True)
    k.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    k.add_argument("--dry-run", action="store_true")

    return p


def main() -> None:
    try:  # SSH/non-interactive session -> run on the real desktop, then exit
        from hub import desktop
    except ImportError:
        import desktop
    desktop.ensure_desktop("apps", sys.argv[1:])

    args = build_parser().parse_args()
    if args.action == "focus" and not any(
            v is not None for v in (args.title, args.pid, args.hwnd)):
        emit(False, "focus", error="need --title, --pid, or --hwnd")
    if args.action == "close" and not getattr(args, "name", None) and not any(
            v is not None for v in (args.title, args.pid, args.hwnd)):
        emit(False, "close", error="need an app name, or --title/--pid/--hwnd")
    handler = {"list": do_list, "query": do_query, "launch": do_launch,
               "open": do_open, "focus": do_focus, "close": do_close,
               "kill": do_kill}[args.action]
    try:
        assert_alive(f"apps.{args.action}")
        handler(args)
    except KillSwitchEngaged as e:
        emit(False, args.action, error=str(e))
    except Exception as e:  # last-resort net: still emit structured JSON
        emit(False, args.action, error=f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
