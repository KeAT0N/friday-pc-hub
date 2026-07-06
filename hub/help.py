"""
hub/help.py — human-facing command cheat sheet for the Remote Hub.

Prints every hub command, how to reach the project folder, and how to launch
Claude Code — so the whole system is easy to drive from a phone terminal, and
you can always ask Claude for something we haven't built a command for.

Run:  hub help   (or just  hub)
"""

from __future__ import annotations

from pathlib import Path

REPO = str(Path(__file__).resolve().parent.parent)

HELP = r"""
Remote Hub - command cheat sheet
================================
Run from anywhere as:   hub <module> <command> [options]
(shortcut for:          python -m hub.<module> <command>)

GET TO THE PROJECT / LAUNCH CLAUDE
----------------------------------
  cd __REPO__
  claude                 start Claude Code in the project - ask for ANYTHING
                         we haven't built a command for (open an app, edit a
                         file, look something up, build a new tool, etc.)

FRIDAY  (scenes / orchestration)
  hub friday status                     health dashboard (read-only)
  hub friday respond [--dry-run]        run the security tripwire now
  hub friday trigger goodnight [--dry-run]   wind down + lock
  hub friday trigger chill | sit-down | optimize [--dry-run]
  hub friday boot --profile dev [--dry-run]
  hub friday watch status               is the auto tripwire registered?

SECURITY  (your PC guard)
  hub security audit                    posture scorecard
  hub security intruders                recent failed logins / threats
  hub security scan [--full] --confirm  Defender scan          (admin)
  hub security firewall-on --confirm                           (admin)
  hub security realtime-on --confirm                           (admin)

POWER / SESSION
  hub power status
  hub power lock --confirm
  hub power monitor-off --confirm
  hub power sleep | hibernate | shutdown | restart --confirm
  hub power cancel                      abort a pending shutdown

APPS & WINDOWS
  hub apps list
  hub apps launch --target <exe|url>
  hub apps focus | close --title "..."
  hub window list
  hub window snap --title "..." --to left|right|maximize|center

SYSTEM / NETWORK / SCREEN
  hub system snapshot
  hub net status | adapters | wifi | ping --host <h>
  hub screen displays
  hub screen capture                    screenshot -> your Pictures

CLIPBOARD / MEDIA / FILES
  hub clipboard get [--reveal]
  hub clipboard set --text "..." --confirm
  hub media play-pause | next | prev | mute | volume-up [--steps N]
  hub files roots | search --pattern "*.pdf" | read --path "..."

NOTIFICATIONS
  hub push test                         buzz your phone
  hub notify send --title T --message M desktop toast

SAFETY
  hub safety kill status                is the hub frozen?
  hub safety kill engage --reason "..." freeze everything
  hub safety kill disengage             unfreeze

NOTES
  * Anything that changes the system needs   --confirm
  * Some security actions need an admin terminal (run at the PC)
  * Freeze the whole hub instantly: create the file   hub\KILLSWITCH
  * Vault / mail read only at the PC (SSH sessions can't unlock DPAPI)
"""


def main() -> None:
    print(HELP.strip().replace("__REPO__", REPO))


if __name__ == "__main__":
    main()
