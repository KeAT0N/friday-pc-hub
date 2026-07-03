"""
hub/notify.py — write-only desktop notifications for the Remote Hub.

Fires native Windows toast notifications via winotify (WinRT toast API).
Write-only: this module puts text on the screen and reports whether it did;
it reads nothing and returns nothing but the standard envelope.

Input hygiene: title/message are stripped of control characters and clamped
to fixed ceilings (honest `truncated` flags, mirroring files.read). The show
call is deadline-bounded so a wedged toast pipeline can never hang the hub.

CLI:
    send --title T --message M [--duration short|long] [--silent]
"""

from __future__ import annotations

import argparse
import json
import sys

from winotify import Notification, audio

try:  # package import or direct script run
    from hub.safety import KillSwitchEngaged, assert_alive, deadline
except ImportError:
    from safety import KillSwitchEngaged, assert_alive, deadline

APP_ID = "Remote Hub"
TITLE_MAX = 64
MESSAGE_MAX = 512
SHOW_DEADLINE_SEC = 10.0


def emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=True,  # survives cp1252 consoles; parsers decode \uXXXX
    ))
    sys.exit(0 if ok else 1)


def _sanitize(text: str, cap: int) -> tuple[str, bool]:
    """Strip control chars (newline survives), clamp to cap, report clamping."""
    clean = "".join(ch for ch in text if ch >= " " or ch == "\n").strip()
    return clean[:cap], len(clean) > cap


@deadline(SHOW_DEADLINE_SEC)
def _show(title: str, message: str, duration: str, silent: bool) -> None:
    toast = Notification(app_id=APP_ID, title=title, msg=message,
                         duration=duration)
    toast.set_audio(audio.Silent if silent else audio.Default, loop=False)
    toast.show()


def do_send(args) -> None:
    title, title_truncated = _sanitize(args.title, TITLE_MAX)
    message, message_truncated = _sanitize(args.message, MESSAGE_MAX)
    if not title:
        emit(False, "send", error="title is empty after sanitization")

    _show(title, message, args.duration, args.silent)
    emit(True, "send", data={
        "app_id": APP_ID, "title": title,
        "title_truncated": title_truncated,
        "message_chars": len(message),
        "message_truncated": message_truncated,
        "duration": args.duration, "silent": args.silent,
    })


def main() -> None:
    p = argparse.ArgumentParser(prog="hub.notify", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    s = sub.add_parser("send")
    s.add_argument("--title", required=True)
    s.add_argument("--message", required=True)
    s.add_argument("--duration", choices=["short", "long"], default="short")
    s.add_argument("--silent", action="store_true")

    args = p.parse_args()
    try:
        assert_alive(f"notify.{args.action}")
        do_send(args)
    except KillSwitchEngaged as e:
        emit(False, args.action, error=str(e))
    except Exception as e:
        emit(False, args.action, error=f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
