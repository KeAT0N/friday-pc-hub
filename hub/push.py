"""
hub/push.py — phone push notifications via ntfy for the Remote Hub.

Pushes a message to the user's phone (ntfy app) through ntfy.sh or a
self-hosted server. The topic name is the shared secret, so it is kept OUT of
git — stored in a gitignored hub/.push.json written by `configure`, or
overridden by env (HUB_NTFY_TOPIC / HUB_NTFY_SERVER). Fail-soft / inert until
configured, so wiring it into respond never breaks an unconfigured hub.

Bounded: the HTTP POST carries a timeout. Header values are ASCII-sanitized
(the ntfy Title/Tags headers must be latin-1); the message body is UTF-8.

Dependency-free: stdlib urllib only.

CLI:
    configure --topic T [--server S]     write the gitignored push config
    status                               is a topic configured? (topic masked)
    send --title T --message M [--priority min|low|default|high|urgent] [--tags T]
    test                                 send a test push
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

try:  # package import or direct script run
    from hub.safety import KillSwitchEngaged, assert_alive
except ImportError:
    from safety import KillSwitchEngaged, assert_alive

DEFAULT_SERVER = "https://ntfy.sh"
CONFIG_PATH = Path(__file__).resolve().parent / ".push.json"
PUSH_TIMEOUT = 10.0
TITLE_MAX = 120
MSG_MAX = 1024
PRIORITIES = ("min", "low", "default", "high", "urgent")


def emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=True,  # survives cp1252 consoles; parsers decode \uXXXX
    ))
    sys.exit(0 if ok else 1)


def _ascii_header(text: str, cap: int) -> str:
    """Strip control chars and non-latin-1 so a header value can't break the
    HTTP request; clamp to cap."""
    clean = "".join(ch for ch in text if " " <= ch < "\x7f").strip()
    return clean[:cap]


def _mask(topic: str | None) -> str | None:
    if not topic:
        return None
    return topic[:6] + "…" if len(topic) > 6 else "set"


def load_config() -> dict:
    """Env override wins; else the gitignored file; else server default + no topic."""
    topic = os.environ.get("HUB_NTFY_TOPIC")
    server = os.environ.get("HUB_NTFY_SERVER")
    if topic:
        return {"server": server or DEFAULT_SERVER, "topic": topic}
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        cfg = {}
    return {"server": server or cfg.get("server") or DEFAULT_SERVER,
            "topic": cfg.get("topic")}


def post(cfg: dict, title: str, message: str, priority: str, tags: str) -> int:
    url = f"{cfg['server'].rstrip('/')}/{cfg['topic']}"
    headers = {"Title": _ascii_header(title, TITLE_MAX), "Priority": priority}
    if tags:
        headers["Tags"] = _ascii_header(tags, 64)
    body = message[:MSG_MAX].encode("utf-8")   # body may be UTF-8
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=PUSH_TIMEOUT) as resp:
        return resp.status


# ---------------------------------------------------------------- actions

def do_configure(args) -> None:
    server = args.server or DEFAULT_SERVER
    CONFIG_PATH.write_text(json.dumps({"server": server, "topic": args.topic}),
                           encoding="utf-8")
    emit(True, "configure", data={"server": server,
                                  "topic_masked": _mask(args.topic),
                                  "config_file": str(CONFIG_PATH)})


def do_status(args) -> None:
    cfg = load_config()
    emit(True, "status", data={"configured": bool(cfg["topic"]),
                               "server": cfg["server"],
                               "topic_masked": _mask(cfg["topic"])})


def do_send(args) -> None:
    cfg = load_config()
    if not cfg["topic"]:
        emit(False, "send", error="no ntfy topic configured — push layer inert "
             "(run `push configure --topic ...` or set HUB_NTFY_TOPIC)")
    try:
        status = post(cfg, args.title, args.message, args.priority, args.tags)
    except (urllib.error.URLError, OSError) as e:
        emit(False, "send", error=f"push failed: {type(e).__name__}: {e}")
    emit(True, "send", data={"server": cfg["server"],
                             "topic_masked": _mask(cfg["topic"]),
                             "http_status": status,
                             "title": _ascii_header(args.title, TITLE_MAX)})


def main() -> None:
    p = argparse.ArgumentParser(prog="hub.push", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    c = sub.add_parser("configure")
    c.add_argument("--topic", required=True)
    c.add_argument("--server")

    sub.add_parser("status")

    s = sub.add_parser("send")
    s.add_argument("--title", required=True)
    s.add_argument("--message", required=True)
    s.add_argument("--priority", choices=PRIORITIES, default="default")
    s.add_argument("--tags", default="")

    sub.add_parser("test")

    args = p.parse_args()
    try:
        assert_alive(f"push.{args.action}")
        if args.action == "configure":
            do_configure(args)
        elif args.action == "status":
            do_status(args)
        elif args.action == "send":
            do_send(args)
        elif args.action == "test":
            args.title = "Remote Hub"
            args.message = "Test push from your PC. If you see this, alerts work."
            args.priority = "default"
            args.tags = "white_check_mark"
            do_send(args)
    except KillSwitchEngaged as e:
        emit(False, args.action, error=str(e))
    except Exception as e:
        emit(False, args.action, error=f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
