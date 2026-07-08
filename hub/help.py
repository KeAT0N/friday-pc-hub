"""
hub/help.py — beginner-friendly command guide for the Remote Hub.

Plain-English, grouped by what you want to do, with an example for every
command — meant to be learned from. Prints on `hub help` (or just `hub`).
"""

from __future__ import annotations

from pathlib import Path

REPO = str(Path(__file__).resolve().parent.parent)

HELP = r"""
============================================================
  REMOTE HUB  -  your PC, controlled from one simple command
============================================================

HOW IT WORKS
  Every command looks like:   hub <thing> <action> [options]
  Example:                    hub power lock --confirm
                                  |     |       |
                                 area  what    "yes, do it"

  * Read-only commands (status, list, audit...) are always safe.
  * Anything that CHANGES your PC needs  --confirm  on the end.
    No --confirm = it just shows you what it WOULD do (a preview).
  * Type  hub help  any time to see this again.

------------------------------------------------------------
 OPEN & CLOSE APPS
------------------------------------------------------------
  hub apps open spotify        Open an app by name. Also works with:
                               chrome, discord, steam, epic, notepad,
                               calculator, settings, files, edge...
  hub apps open <name>         Don't see it above? Just try the name.

  hub apps close spotify       Close an app's window (asks it nicely -
                               if it has unsaved work it stays open).
  hub apps list                See every open window right now.

  Note: "close" shuts the WINDOW. A few apps keep running quietly in
  the background (tray). To fully quit one of those:
  hub apps kill --name discord.exe

------------------------------------------------------------
 CHECK ON YOUR PC  (all safe / read-only)
------------------------------------------------------------
  hub friday status            The big dashboard: CPU, memory, disk,
                               internet, unread mail - all at once.
  hub system snapshot          Detailed system stats.
  hub apps list                What windows are open.
  hub net status               Are you online? What's your IP?

------------------------------------------------------------
 KEEP YOUR PC SAFE  (security)
------------------------------------------------------------
  hub security audit           Safety report card: antivirus, firewall,
                               etc. Tells you if anything's off.
  hub security intruders       Recent failed logins / threats.
  hub security scan --confirm  Run a virus scan (needs admin PC window).

  (Your PC also auto-watches for intruders and buzzes your phone.)

------------------------------------------------------------
 POWER & SESSION  (need --confirm)
------------------------------------------------------------
  hub power lock --confirm         Lock the screen.
  hub power monitor-off --confirm  Turn the display off.
  hub power sleep --confirm        Put the PC to sleep.
  hub power shutdown --confirm     Shut down (60s delay).
  hub power restart --confirm      Restart.
  hub power cancel                 Cancel a pending shutdown.
  hub power status                 On AC/battery? (safe)

------------------------------------------------------------
 WINDOWS & MEDIA
------------------------------------------------------------
  hub window snap --title Chrome --to left     Snap a window left/
                          (or: right, maximize, center, top-left...)
  hub media play-pause         Play / pause music or video.
  hub media next  /  prev      Skip track.
  hub media volume-up --steps 3   Volume up (3 notches). Also mute.

------------------------------------------------------------
 SCREEN / CLIPBOARD / FILES
------------------------------------------------------------
  hub screen capture           Screenshot -> saved in your Pictures.
  hub clipboard get            See what's copied (hides passwords).
  hub files search --pattern "*.pdf"   Find files by name.

------------------------------------------------------------
 PHONE ALERTS
------------------------------------------------------------
  hub push test                Send a test buzz to your phone.

------------------------------------------------------------
 EMERGENCY STOP
------------------------------------------------------------
  hub safety kill engage --reason "stop"   Freeze the WHOLE hub -
                               every command refuses until you undo it.
  hub safety kill disengage    Un-freeze.
  hub safety kill status       Is it frozen right now?

------------------------------------------------------------
 WANT SOMETHING NOT LISTED HERE?  Ask Claude directly:
------------------------------------------------------------
  cd __REPO__
  claude
    ...then just type what you want, e.g. "open Spotify and play my
    Discover Weekly", or "add a hub command that does X". Claude runs
    in this project and can do anything - even build new commands.

============================================================
 TIP: not sure about a command? Run it WITHOUT --confirm first to see
 a safe preview of what it would do. Nothing changes until you confirm.
============================================================
"""


def main() -> None:
    print(HELP.strip().replace("__REPO__", REPO))


if __name__ == "__main__":
    main()
