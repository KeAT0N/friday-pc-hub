"""
hub/screen.py — screen capture for the Remote Hub.

`displays` is read-only monitor enumeration. `capture` grabs the screen (whole
virtual desktop or one monitor) via GDI BitBlt, encodes a PNG by hand with
stdlib zlib (no image-library dependency), and writes it into an allowed
files.py root — returning the staged path + sha256 so the orchestrator can
send it to the phone. Image bytes are NEVER written to stdout.

Contained + non-destructive: output lands under a permitted files root
(Pictures/RemoteHub by preference), with a timestamped, uniquified name so a
capture never overwrites an existing file. Bounded by construction — a single
BitBlt + an in-memory encode.

Dependency-free: pywin32 (win32gui/win32ui/win32api) + stdlib zlib/struct.

CLI:
    displays                         read-only: monitors, geometry, primary flag
    capture [--display N | --all]    default --all (virtual desktop)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import struct
import sys
import time
import zlib
from pathlib import Path

import win32api
import win32con
import win32gui

try:  # package import or direct script run
    from hub.files import allowed_roots
    from hub.safety import KillSwitchEngaged, assert_alive
except ImportError:
    from files import allowed_roots
    from safety import KillSwitchEngaged, assert_alive

OUTPUT_SUBDIR = "RemoteHub"
MONITORINFOF_PRIMARY = 1


def emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=True,  # survives cp1252 consoles; parsers decode \uXXXX
    ))
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------- png (pure)

def _encode_png(width: int, height: int, rgba: bytes) -> bytes:
    """Encode top-down RGBA bytes as a PNG (color type 6). Pure stdlib."""
    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (struct.pack(">I", len(data)) + body
                + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    stride = width * 4
    raw = bytearray()
    for y in range(height):                    # prefix each scanline: filter 0
        raw += b"\x00"
        raw += rgba[y * stride:(y + 1) * stride]
    idat = zlib.compress(bytes(raw), 6)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", idat) + chunk(b"IEND", b""))


def _bgrx_to_rgba(bits: bytes) -> bytearray:
    """Swap B/R (BGRX -> RGBA) and force opaque alpha, at C-level slice speed."""
    a = bytearray(bits)
    a[0::4], a[2::4] = bytes(a[2::4]), bytes(a[0::4])   # B <-> R
    a[3::4] = b"\xff" * (len(a) // 4)                    # alpha := 255
    return a


# ---------------------------------------------------------------- capture (win32)

def _grab(rect: tuple[int, int, int, int]) -> tuple[int, int, bytearray]:
    """BitBlt the given (left, top, width, height) region -> (w, h, rgba)."""
    import win32ui  # lazy: displays/enumeration must work even without win32ui
    left, top, width, height = rect
    if width <= 0 or height <= 0:
        raise ValueError(f"non-positive capture region {rect}")
    hdesktop = win32gui.GetDesktopWindow()
    hwindc = win32gui.GetWindowDC(hdesktop)
    srcdc = win32ui.CreateDCFromHandle(hwindc)
    memdc = srcdc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    try:
        bmp.CreateCompatibleBitmap(srcdc, width, height)
        memdc.SelectObject(bmp)
        memdc.BitBlt((0, 0), (width, height), srcdc, (left, top),
                     win32con.SRCCOPY)
        bits = bmp.GetBitmapBits(True)          # BGRX, top-down
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        memdc.DeleteDC()
        srcdc.DeleteDC()
        win32gui.ReleaseDC(hdesktop, hwindc)
    return width, height, _bgrx_to_rgba(bits)


def _monitors() -> list[dict]:
    out = []
    for i, (hmon, _hdc, _rect) in enumerate(win32api.EnumDisplayMonitors()):
        info = win32api.GetMonitorInfo(hmon)
        out.append({
            "index": i,
            "device": info.get("Device"),
            "rect": list(info["Monitor"]),
            "work": list(info["Work"]),
            "primary": bool(info["Flags"] & MONITORINFOF_PRIMARY),
        })
    return out


def _virtual_screen() -> tuple[int, int, int, int]:
    gm = win32api.GetSystemMetrics
    return (gm(win32con.SM_XVIRTUALSCREEN), gm(win32con.SM_YVIRTUALSCREEN),
            gm(win32con.SM_CXVIRTUALSCREEN), gm(win32con.SM_CYVIRTUALSCREEN))


def _output_dir() -> Path:
    roots = allowed_roots()
    if not roots:
        emit(False, "capture", error="no allowed files root to save into")
    base = next((r for r in roots if r.name.lower() == "pictures"), roots[0])
    d = base / OUTPUT_SUBDIR
    d.mkdir(exist_ok=True)
    return d


UNIQUE_CAP = 10_000   # ceiling on the same-second de-dup loop


def _unique_path(directory: Path) -> Path:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    candidate = directory / f"screen_{stamp}.png"
    n = 1
    while candidate.exists() and n <= UNIQUE_CAP:   # bounded, not open-ended
        candidate = directory / f"screen_{stamp}_{n}.png"
        n += 1
    if candidate.exists():   # astronomically unlikely; guarantee termination
        candidate = directory / f"screen_{stamp}_{os.getpid()}_{n}.png"
    return candidate


# ---------------------------------------------------------------- actions

def do_capture(args) -> None:
    if args.display is not None:
        mons = _monitors()
        if not (0 <= args.display < len(mons)):
            emit(False, "capture",
                 error=f"display {args.display} out of range (have {len(mons)})")
        l, t, r, b = mons[args.display]["rect"]
        rect, label = (l, t, r - l, b - t), f"display {args.display}"
    else:
        rect, label = _virtual_screen(), "all"

    width, height, rgba = _grab(rect)
    png = _encode_png(width, height, rgba)

    out_dir = _output_dir()
    path = _unique_path(out_dir)
    # contained by construction (out_dir is under an allowed root), assert anyway
    if not any(root == path.parent or root in path.parents
               for root in allowed_roots()):
        emit(False, "capture", error="refusing to write outside allowed roots")
    path.write_bytes(png)
    emit(True, "capture", data={
        "path": str(path), "bytes": len(png),
        "sha256": hashlib.sha256(png).hexdigest(),
        "width": width, "height": height, "display": label,
    })


def main() -> None:
    p = argparse.ArgumentParser(prog="hub.screen", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("displays")
    c = sub.add_parser("capture")
    g = c.add_mutually_exclusive_group()
    g.add_argument("--display", type=int, help="capture only this monitor index")
    g.add_argument("--all", action="store_true", help="whole virtual desktop (default)")

    args = p.parse_args()
    try:
        assert_alive(f"screen.{args.action}")
        if args.action == "displays":
            emit(True, "displays", data=_monitors())
        else:
            if not hasattr(args, "display"):
                args.display = None
            do_capture(args)
    except KillSwitchEngaged as e:
        emit(False, args.action, error=str(e))
    except Exception as e:
        emit(False, args.action, error=f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
