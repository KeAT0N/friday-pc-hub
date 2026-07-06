"""
hub/clipboard.py — clipboard access for the Remote Hub.

get is read-only; set/clear are mutations and are confirm-gated (dry-run
preview by default, like mail's send) because writing the clipboard clobbers
whatever the user had copied.

Secret hygiene: the clipboard often holds a freshly-copied password or token.
`get` runs a conservative heuristic and WITHHOLDS content that looks sensitive
(content=null, looks_sensitive=true) unless --reveal is passed — so a hub
read never dumps a copied secret into chat by accident. Reads are length-capped
with an honest `truncated` flag.

Dependency-free beyond pywin32 (win32clipboard). Clipboard opens are bounded by
a short deadline with retry, so a momentarily-locked clipboard can't hang us.

CLI:
    get   [--max-chars N] [--reveal]
    set   --text T [--confirm]
    clear [--confirm]
"""

from __future__ import annotations

import argparse
import json
import sys
import time

import win32clipboard
import win32con

try:  # package import or direct script run
    from hub.safety import Deadline, KillSwitchEngaged, assert_alive
except ImportError:
    from safety import Deadline, KillSwitchEngaged, assert_alive

READ_DEFAULT_CHARS = 4096
READ_HARD_CAP = 65_536
SET_MAX_CHARS = 65_536
OPEN_DEADLINE_SEC = 2.0
OPEN_RETRY_SLEEP = 0.05


def emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=True,  # survives cp1252 consoles; parsers decode \uXXXX
    ))
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------- heuristic

def _looks_sensitive(text: str) -> bool:
    """Conservative: better to withhold a URL than surface a password.

    Trips on PEM key blocks, secret-ish keywords, or a single opaque token
    (no whitespace, 16-512 chars, mixed character classes)."""
    if not text:
        return False
    if "-----BEGIN" in text and "PRIVATE KEY" in text:
        return True
    low = text.lower()
    if any(k in low for k in ("password", "passwd", "secret", "api_key",
                              "apikey", "token", "bearer")):
        return True
    s = text.strip()
    if 16 <= len(s) <= 512 and not any(c.isspace() for c in s):
        classes = sum([any(c.islower() for c in s), any(c.isupper() for c in s),
                       any(c.isdigit() for c in s),
                       any(not c.isalnum() for c in s)])
        if classes >= 2:
            return True
    return False


# ---------------------------------------------------------------- clipboard io

def _open_bounded() -> None:
    """OpenClipboard with bounded retry — another process may hold it briefly."""
    dl = Deadline(OPEN_DEADLINE_SEC)
    while True:
        try:
            win32clipboard.OpenClipboard()
            return
        except Exception:
            if dl.expired:
                raise
            time.sleep(OPEN_RETRY_SLEEP)


def _get_text() -> str | None:
    _open_bounded()
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return None
    finally:
        win32clipboard.CloseClipboard()


def _set_text(text: str) -> None:
    _open_bounded()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def _empty() -> None:
    _open_bounded()
    try:
        win32clipboard.EmptyClipboard()
    finally:
        win32clipboard.CloseClipboard()


# ---------------------------------------------------------------- actions

def do_get(args) -> None:
    text = _get_text()
    if text is None:
        emit(True, "get", data={"available": False, "length": 0,
                                "content": None})
    budget = min(args.max_chars, READ_HARD_CAP)
    sensitive = _looks_sensitive(text)
    withheld = sensitive and not args.reveal
    shown = None if withheld else text[:budget]
    emit(True, "get", data={
        "available": True,
        "length": len(text),
        "looks_sensitive": sensitive,
        "withheld": withheld,
        "truncated": (not withheld) and len(text) > budget,
        "encoding": "unicode",
        "content": shown,
    })


def do_set(args) -> None:
    text = args.text
    if len(text) > SET_MAX_CHARS:
        emit(False, "set",
             error=f"text exceeds {SET_MAX_CHARS} chars ({len(text)})")
    if not args.confirm:
        emit(True, "set", data={"dry_run": True, "confirm_required": True,
                                "chars": len(text)})
    _set_text(text)
    emit(True, "set", data={"confirmed": True, "chars": len(text)})


def do_clear(args) -> None:
    if not args.confirm:
        emit(True, "clear", data={"dry_run": True, "confirm_required": True})
    _empty()
    emit(True, "clear", data={"confirmed": True})


# ---------------------------------------------------------------- cli

def main() -> None:
    p = argparse.ArgumentParser(prog="hub.clipboard", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    g = sub.add_parser("get")
    g.add_argument("--max-chars", type=int, default=READ_DEFAULT_CHARS)
    g.add_argument("--reveal", action="store_true",
                   help="return content even if it looks like a secret")

    s = sub.add_parser("set")
    s.add_argument("--text", required=True)
    s.add_argument("--confirm", action="store_true")

    c = sub.add_parser("clear")
    c.add_argument("--confirm", action="store_true")

    args = p.parse_args()
    handler = {"get": do_get, "set": do_set, "clear": do_clear}[args.action]
    try:
        assert_alive(f"clipboard.{args.action}")
        handler(args)
    except KillSwitchEngaged as e:
        emit(False, args.action, error=str(e))
    except Exception as e:
        emit(False, args.action, error=f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
