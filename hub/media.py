"""
hub/media.py — media transport & volume keys for the Remote Hub.

Sends the standard Windows media virtual-keys (play/pause, next, prev, stop,
volume up/down, mute) via keybd_event. The shell routes them to whatever app
owns the current media session, so no target window is needed.

Confirm-gate exemption (deliberate): unlike power/mail mutations, these act
DIRECTLY with no --confirm. A media key is benign and instantly reversible
(press play again, nudge the volume back) — it causes no data loss and no
outward/irreversible effect, so gating "next track" behind a confirm would be
pure friction. The disruptive verbs live in power.py, which IS gated.

Dependency-free: ctypes -> user32.keybd_event.

CLI:
    keys                                  read-only: list supported keys + codes
    play-pause | next | prev | stop
    mute
    volume-up   [--steps N]               each step ~= 2% (N capped)
    volume-down [--steps N]
"""

from __future__ import annotations

import argparse
import ctypes
import json
import sys

try:  # package import or direct script run
    from hub.safety import KillSwitchEngaged, assert_alive
except ImportError:
    from safety import KillSwitchEngaged, assert_alive

# Virtual-key codes for the media/volume keys.
VK = {
    "play-pause": 0xB3,   # VK_MEDIA_PLAY_PAUSE
    "next": 0xB0,         # VK_MEDIA_NEXT_TRACK
    "prev": 0xB1,         # VK_MEDIA_PREV_TRACK
    "stop": 0xB2,         # VK_MEDIA_STOP
    "mute": 0xAD,         # VK_VOLUME_MUTE (toggles)
    "volume-up": 0xAF,    # VK_VOLUME_UP
    "volume-down": 0xAE,  # VK_VOLUME_DOWN
}
STEPPED = {"volume-up", "volume-down"}   # verbs that accept --steps
STEP_CAP = 50
KEYEVENTF_KEYUP = 0x0002


def emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=True,  # survives cp1252 consoles; parsers decode \uXXXX
    ))
    sys.exit(0 if ok else 1)


def _tap(vk: int) -> None:
    """Press and release one virtual key."""
    user32 = ctypes.windll.user32
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, 0)


def do_keys() -> None:
    emit(True, "keys", data={
        "keys": [{"verb": v, "vk": hex(c), "stepped": v in STEPPED}
                 for v, c in VK.items()]})


def do_send(args) -> None:
    verb = args.action
    steps = getattr(args, "steps", 1) if verb in STEPPED else 1
    steps = max(1, min(steps, STEP_CAP))
    for _ in range(steps):
        _tap(VK[verb])
    emit(True, verb, data={"sent": verb, "vk": hex(VK[verb]), "repeats": steps})


def main() -> None:
    try:  # SSH/non-interactive session -> run on the real desktop, then exit
        from hub import desktop
    except ImportError:
        import desktop
    desktop.ensure_desktop("media", sys.argv[1:])

    p = argparse.ArgumentParser(prog="hub.media", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("keys")
    for verb in ("play-pause", "next", "prev", "stop", "mute"):
        sub.add_parser(verb)
    for verb in ("volume-up", "volume-down"):
        s = sub.add_parser(verb)
        s.add_argument("--steps", type=int, default=1,
                       help=f"repeat the key N times (1-{STEP_CAP}); "
                            "each tap nudges the volume one notch")

    args = p.parse_args()
    try:
        assert_alive(f"media.{args.action}")
        if args.action == "keys":
            do_keys()
        else:
            do_send(args)
    except KillSwitchEngaged as e:
        emit(False, args.action, error=str(e))
    except Exception as e:
        emit(False, args.action, error=f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
