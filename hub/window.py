"""
hub/window.py — window layout control for the Remote Hub.

Move, resize, and snap the visible top-level windows enumerated by apps.py.
Follows apps.py's model: a target is resolved to EXACTLY one window (ambiguity
is a structured error listing candidates, never a guess), then the layout op
acts directly — the same posture as apps focus/close (a reposition is benign
and reversible; the disruptive/gated verbs live in power.py).

Snap presets are computed from the window's monitor work area (taskbar
excluded) via GetMonitorInfo, so halves/quadrants land correctly on whichever
display the window is on. Each op is a single bounded Win32 call.

Dependency-free beyond pywin32 (win32gui/win32api) + the shared apps enumerator.

CLI (target = one of --hwnd / --title / --pid):
    list                                         read-only: windows + geometry
    move   <target> --x X --y Y [--width W --height H]
    resize <target> --width W --height H
    snap   <target> --to PRESET
        PRESET: left right top bottom
                top-left top-right bottom-left bottom-right
                maximize minimize restore center full
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict

import win32api
import win32con
import win32gui

try:  # package import or direct script run
    from hub.apps import enum_windows
    from hub.safety import KillSwitchEngaged, assert_alive
except ImportError:
    from apps import enum_windows
    from safety import KillSwitchEngaged, assert_alive

SWP_FLAGS = win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE
POSITIONAL = ("left", "right", "top", "bottom", "top-left", "top-right",
              "bottom-left", "bottom-right", "full")
COMMANDS = ("maximize", "minimize", "restore", "center")
PRESETS = POSITIONAL + COMMANDS


def emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=True,  # survives cp1252 consoles; parsers decode \uXXXX
    ))
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------- geometry (pure)

def _snap_geometry(work: tuple[int, int, int, int], preset: str):
    """Map a positional preset to (x, y, w, h) within a work area, or None.

    Pure function (no Win32) so the layout math is unit-testable in isolation.
    """
    left, top, right, bottom = work
    width, height = right - left, bottom - top
    hw, hh = width // 2, height // 2
    return {
        "left": (left, top, hw, height),
        "right": (left + hw, top, width - hw, height),
        "top": (left, top, width, hh),
        "bottom": (left, top + hh, width, height - hh),
        "top-left": (left, top, hw, hh),
        "top-right": (left + hw, top, width - hw, hh),
        "bottom-left": (left, top + hh, hw, height - hh),
        "bottom-right": (left + hw, top + hh, width - hw, height - hh),
        "full": (left, top, width, height),
    }.get(preset)


# ---------------------------------------------------------------- win32 helpers
# Individually patchable so tests never move the user's real windows.

def _get_rect(hwnd: int) -> tuple[int, int, int, int]:
    return win32gui.GetWindowRect(hwnd)


def _work_area(hwnd: int) -> tuple[int, int, int, int]:
    mon = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
    return win32api.GetMonitorInfo(mon)["Work"]


def _is_maximized(hwnd: int) -> bool:
    return win32gui.GetWindowPlacement(hwnd)[1] == win32con.SW_SHOWMAXIMIZED


def _set_pos(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    win32gui.SetWindowPos(hwnd, 0, x, y, w, h, SWP_FLAGS)


def _show(hwnd: int, cmd: int) -> None:
    win32gui.ShowWindow(hwnd, cmd)


# ---------------------------------------------------------------- resolve

def resolve(args) -> int:
    """Resolve --hwnd/--title/--pid to exactly one hwnd, or emit failure."""
    if args.hwnd is not None:
        if not win32gui.IsWindow(args.hwnd):
            emit(False, args.action, error=f"no window with hwnd {args.hwnd}")
        return args.hwnd
    matches = enum_windows(title=args.title, pid=args.pid)
    if not matches:
        emit(False, args.action, error="no window matched",
             data={"title": args.title, "pid": args.pid})
    if len(matches) > 1:
        emit(False, args.action,
             error=f"{len(matches)} windows matched; disambiguate with --hwnd",
             data=[asdict(m) for m in matches])
    return matches[0].hwnd


# ---------------------------------------------------------------- actions

def do_list(args) -> None:
    out = []
    for w in enum_windows():
        try:
            rect = _get_rect(w.hwnd)
        except win32gui.error:
            continue
        out.append({**asdict(w), "rect": {"left": rect[0], "top": rect[1],
                                          "right": rect[2], "bottom": rect[3]},
                    "maximized": _is_maximized(w.hwnd)})
    emit(True, "list", data=out)


def do_move(args) -> None:
    hwnd = resolve(args)
    if _is_maximized(hwnd):
        _show(hwnd, win32con.SW_RESTORE)
    left, top, right, bottom = _get_rect(hwnd)
    w = args.width if args.width is not None else right - left
    h = args.height if args.height is not None else bottom - top
    _set_pos(hwnd, args.x, args.y, w, h)
    emit(True, "move", data={"hwnd": hwnd, "x": args.x, "y": args.y,
                             "width": w, "height": h})


def do_resize(args) -> None:
    hwnd = resolve(args)
    if _is_maximized(hwnd):
        _show(hwnd, win32con.SW_RESTORE)
    left, top, _, _ = _get_rect(hwnd)
    _set_pos(hwnd, left, top, args.width, args.height)
    emit(True, "resize", data={"hwnd": hwnd, "width": args.width,
                               "height": args.height})


def do_snap(args) -> None:
    hwnd = resolve(args)
    preset = args.to

    if preset in ("maximize", "minimize", "restore"):
        cmd = {"maximize": win32con.SW_MAXIMIZE,
               "minimize": win32con.SW_MINIMIZE,
               "restore": win32con.SW_RESTORE}[preset]
        _show(hwnd, cmd)
        emit(True, "snap", data={"hwnd": hwnd, "to": preset})

    # positional presets need the window restored first to move cleanly
    if _is_maximized(hwnd):
        _show(hwnd, win32con.SW_RESTORE)
    work = _work_area(hwnd)

    if preset == "center":
        left, top, right, bottom = _get_rect(hwnd)
        w, h = right - left, bottom - top
        wl, wt, wr, wb = work
        x = wl + ((wr - wl) - w) // 2
        y = wt + ((wb - wt) - h) // 2
        _set_pos(hwnd, x, y, w, h)
        emit(True, "snap", data={"hwnd": hwnd, "to": "center",
                                 "x": x, "y": y, "width": w, "height": h})

    geo = _snap_geometry(work, preset)
    if geo is None:
        emit(False, "snap", error=f"unknown preset {preset!r}")
    _set_pos(hwnd, *geo)
    emit(True, "snap", data={"hwnd": hwnd, "to": preset, "x": geo[0],
                             "y": geo[1], "width": geo[2], "height": geo[3]})


# ---------------------------------------------------------------- cli

def _add_target(parser) -> None:
    parser.add_argument("--hwnd", type=int)
    parser.add_argument("--title")
    parser.add_argument("--pid", type=int)


def main() -> None:
    p = argparse.ArgumentParser(prog="hub.window", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("list")

    m = sub.add_parser("move")
    _add_target(m)
    m.add_argument("--x", type=int, required=True)
    m.add_argument("--y", type=int, required=True)
    m.add_argument("--width", type=int)
    m.add_argument("--height", type=int)

    r = sub.add_parser("resize")
    _add_target(r)
    r.add_argument("--width", type=int, required=True)
    r.add_argument("--height", type=int, required=True)

    s = sub.add_parser("snap")
    _add_target(s)
    s.add_argument("--to", required=True, choices=PRESETS)

    args = p.parse_args()
    if args.action != "list" and not any(
            v is not None for v in (args.hwnd, args.title, args.pid)):
        emit(False, args.action, error="need --hwnd, --title, or --pid")
    handler = {"list": do_list, "move": do_move, "resize": do_resize,
               "snap": do_snap}[args.action]
    try:
        assert_alive(f"window.{args.action}")
        handler(args)
    except KillSwitchEngaged as e:
        emit(False, args.action, error=str(e))
    except Exception as e:
        emit(False, args.action, error=f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
