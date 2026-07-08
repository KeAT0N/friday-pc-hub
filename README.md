# Remote Hub

A phone-controlled control system for a Windows 11 PC, built from small, safe,
single-purpose Python CLIs. You reach the machine from a **terminal** — locally,
or from your phone over a private Tailscale network + key-only SSH (e.g. the
Termius app) — and drive it two ways:

- **Directly** — run `hub <module> <command>` yourself (e.g.
  `hub power lock --confirm`). `hub help` lists everything.
- **Through Claude** — launch Claude Code (`claude`) in that terminal (or use
  the Claude app), and Claude becomes the **brain**: it reads your
  natural-language request, chains the module CLIs, parses the JSON they emit,
  evaluates predicates, decides what to do next, and drives the browser via its
  Chrome MCP tools.

Either way the Python modules in `hub/` are deliberately **dumb, reliable
hands** — no daemon, no custom RPC layer, no LLM logic in the scripts. Each is a
stateless CLI that performs one action and prints one JSON envelope. That split
— smart brain, dumb hands — collapses most of the attack surface by construction.

**GUI commands from the phone:** an SSH login lands in a *non-interactive*
Windows session, separate from the logged-in desktop, so actions that touch the
visible screen (open apps, move windows, media keys, screenshots) would fail or
run invisibly. `hub/desktop.py` bridges them transparently: those modules detect
the non-interactive session and hand the command to an on-demand Scheduled Task
running in session 1, which executes it on the real desktop and relays the JSON
back. One-time setup: `hub desktop install --confirm` (no admin needed).

This README is the execution-model contract: whoever operates the hub — you at
the terminal, or a Claude session — should drive it the same way.

```
  ┌──────────────────────┐   Tailscale +     ┌─────────────────────────┐
  │ Phone (Termius/SSH)   │ ──key-only SSH──▶ │  Terminal on the PC      │
  │  — or a local shell   │                   └────────────┬────────────┘
  └──────────────────────┘                                │
                                    ┌───────────────────────┴───────────────────┐
                                    ▼                                            ▼
                           hub <module> <cmd>                    claude  (the brain: parse NL,
                           (you are the driver)                  chain modules, eval predicates,
                                    │                            + Chrome MCP for the browser)
                                    └──────────────────┬──────────────────────────┘
                                                       ▼
                                              ┌──────────────┐
                                              │  hub/*.py    │  dumb, reliable hands:
                                              │  modules     │  one action, one JSON
                                              └──────────────┘  envelope, then exit
```

## Design philosophy

1. **Stateless CLI + one JSON object on stdout.** Every module invocation
   performs exactly one action and prints exactly one envelope:
   `{"ok": bool, "action": str, "data": ..., "error": str|null}` — exit code
   0/1 mirrors `ok`. No sessions, no state files (except the kill-switch),
   crash-safe by default. The orchestrator parses the envelope; scripts never
   parse each other.
2. **Sync over async.** Each action is a short-lived process making
   synchronous Win32/psutil/filesystem calls. Monotonic deadlines provide the
   no-hang guarantee; asyncio would add complexity without adding safety.
   Revisit only if a genuinely concurrent long-lived workflow appears.
3. **Predicate logic lives in the brain.** Modules emit normalized numbers
   (`*_percent` 0–100 whole-machine, `*_gb`, `*_mb`, `*_sec`); the orchestrator
   compares them. No eval, no threshold DSL, no injection surface in the hands.
4. **Fail closed, refuse loudly.** Ambiguity (two windows matching a title) is
   a structured error listing candidates, never a guess. Everything that can
   wait has a deadline; everything that can loop has a ceiling.
5. **Browser = native extension, not scripts.** When Claude is orchestrating,
   navigation/reading/clicking happen through its Chrome MCP tools live; there
   is deliberately no browser-automation module (Selenium-style automation is
   out of scope). Direct-terminal use has no browser leg.
6. **`ensure_ascii=True` everywhere.** JSON envelopes are pure ASCII
   (`\uXXXX` escapes) so cp1252 consoles can never corrupt them.

## Structural security boundaries

These are enforced by construction, not convention:

- **Secrets never hit stdout.** `credentials.py` exposes secrets only
  in-process via `Secret.reveal()`. The CLI is *existence-only* — there is no
  code path that writes a secret value to stdout. The `Secret` wrapper seals
  every implicit channel (repr/str/format/log-interpolation redact; json,
  pickle, copy, len raise; immutable; constant-time equality). Selftest
  (`credentials.py selftest`) proves all eight channels.
- **No blind GUI credential typing.** Authentication is programmatic (API
  token → authenticated request, or the user's own password manager). Claude
  never types passwords into login forms.
- **File territory is an allowlist.** `files.py` resolves paths (symlinks,
  junctions, `..`) *before* checking containment against permitted roots
  (user content dirs by default; `HUB_FILES_ROOTS` to extend). System dirs
  and AppData are unreachable by construction.
- **Key material is refused even inside territory.** Sensitive dot-dirs
  (`.ssh`, `.aws`, `.gnupg`, …) and key-file patterns (`id_rsa*`, `*.pem`,
  `*.pfx`, `*.kdbx`, …) are blocked on read, search, and stage.
- **Bounded output.** Reads hard-cap at 256KB with truncation flags; binaries
  are sniffed (NUL bytes) and refused with metadata instead of content.
- **Global kill-switch.** The marker file `hub/KILLSWITCH` (override:
  `HUB_KILL_FILE`) makes every module CLI refuse before acting, and aborts
  long operations mid-flight (searches poll it every 500 entries; waits poll
  it every cycle). Engage/disengage via `safety.py kill …` or by
  creating/deleting the file in Explorer — no Python required to stop the hub.

## Safety substrate (`hub/safety.py`)

Shared primitives every module routes through:

- `assert_alive(context)` — raises `KillSwitchEngaged` if the switch is on.
- `Deadline(seconds)` — monotonic budget passed down call chains:
  `.remaining()`, `.expired`, `.check()`, `.sleep()` (never oversleeps).
- `@deadline(seconds)` — bounds a whole call via daemon worker thread
  (Windows has no SIGALRM). Bounds the *caller's wait*; an expired worker is
  abandoned and dies with the short-lived process. Don't wrap side effects
  that must not outlive the deadline.
- `@retry(attempts, base_delay, max_delay, factor, jitter, retry_on,
  total_timeout)` — exponential backoff with three ceilings (attempts,
  per-delay, total wall-clock). `KillSwitchEngaged`/`DeadlineExceeded` are
  never retried: retries must not resurrect what safety killed.
- `wait_until(predicate, timeout, poll)` — the kill-aware bounded poll.

```
python hub\safety.py kill engage --reason "..." | kill disengage | kill status
python hub\safety.py selftest        # uses a temp kill file, real switch untouched
```

## Module CLIs

All commands run from the repo root. Every response is the standard envelope.

### hub/apps.py — application control

```
python hub\apps.py list
python hub\apps.py open   <name>                 friendly: spotify | chrome | notepad ...
python hub\apps.py close  <name>                 friendly: close matching windows by app name
python hub\apps.py query  (--pid N | --name S | --title S)
python hub\apps.py launch --target <exe|path|URI> [--args ...]
                          [--wait-title S] [--timeout N]
python hub\apps.py focus  (--title S | --pid N | --hwnd N) [--timeout N]
python hub\apps.py close  (--title S | --pid N | --hwnd N) [--timeout N] [--force]
```

Notes: `open <name>` resolves a small alias table (spotify→`spotify:`,
settings→`ms-settings:`, notepad→`notepad.exe`, …) and falls back to launching
the name as-is; `launch` is the precise form (exe/URI + args + `--wait-title`
readiness). `close <name>` gracefully `WM_CLOSE`s every visible window whose
process matches — never force-kills, so unsaved-work windows are reported kept
(tray-only apps with no window fall to `kill --name`). The precise
`close (--title|--pid|--hwnd)` closes one window and escalates only with
`--force`. Launcher pid often ≠ app pid (UWP brokers); ambiguous precise
matches error out listing candidate hwnds.

### hub/desktop.py — interactive-session bridge

```
python hub\desktop.py status                 registered? am I interactive?
python hub\desktop.py install   [--confirm]  register the bridge task (dry-run default)
python hub\desktop.py uninstall [--confirm]
python hub\desktop.py run <module> <args>    explicitly bridge one command
```

Notes: GUI modules (apps, window, media, screen, clipboard) call
`ensure_desktop()` at the top of `main()`. When already in the interactive
session it's a no-op (runs normally); over SSH it writes the request to a
gitignored queue file, triggers the `RemoteHubDesktop` Scheduled Task (which
runs `desktop worker` in session 1 via `InteractiveToken`), waits for the
result, and relays the module's envelope verbatim. Session detection uses
`WTSGetActiveConsoleSessionId` vs the process session id. The task is on-demand
(no trigger), hidden, least-privilege, and registers without admin. If it isn't
installed, bridged commands fail loudly telling you to run `desktop install`.

### hub/credentials.py — secret boundary (existence-only CLI)

```
python hub\credentials.py providers
python hub\credentials.py check --service S [--account A] [--provider P]
python hub\credentials.py enroll --service S [--account A] [--stdin] [--expect-format F]
python hub\credentials.py unenroll --service S [--account A]
python hub\credentials.py selftest | keyring-selftest
```

In-process use: `get_provider().get(service, account) -> Secret` →
`.reveal()` only at the point of use. Backends: `null` (placeholder) and
`keyring` (Windows Credential Manager, DPAPI at rest, per-user) are active;
`env` stays gated until green-lit. Selection: arg > `HUB_CRED_PROVIDER` > null.

**Session note (DPAPI):** the keyring vault is DPAPI-encrypted, so it can only
be read by a session that unlocked the user's master key — i.e. an interactive
login at the PC. Over an **SSH key-based login** (e.g. from the phone) DPAPI is
locked, so vault reads come back empty. `check` disambiguates this: a keyring
miss carries `vault_readable:false` + a note (rather than implying the
credential is gone), and `friday status` reports "vault unreadable in this
session" instead of "MISSING". Run vault/mail actions at the PC (or via the
scheduled task, which runs in the interactive session).

**Enrollment is local-terminal-only by construction.** `enroll` demands an
interactive terminal and reads the secret via hidden getpass prompt; run
through an orchestration channel (non-TTY stdin) it refuses and prints the
command to run at the PC instead. The terminal check is hardened for Windows,
where `isatty()` wrongly reports the NUL device as a TTY — a `GetConsoleMode`
probe confirms a real console so the gate refuses (never hangs on getpass)
when driven headless. Secrets therefore never appear in stdout, logs, argv,
process lists, or shell history. `--stdin` permits piping from another local
process for scripted enrollment (BOM-stripped so a shell-injected byte-order
mark never becomes part of the secret). `--expect-format` validates the
secret's shape before storing (named `app-password` = iCloud
`xxxx-xxxx-xxxx-xxxx`, or a custom full-match regex) and refuses to store a
mismatch — nothing is written and the value is never printed. Verification is
an in-process constant-time read-back compare; only booleans are emitted. `keyring-selftest` proves the
vault round-trip with a random canary that is generated, compared, and
deleted without ever being printed.

### hub/system.py — read-only telemetry

```
python hub\system.py snapshot [--top N] [--sample S]
python hub\system.py cpu [--sample S] | memory | disk | network | battery
python hub\system.py top [--by cpu|memory] [--count N] [--sample S]
```

Notes: process CPU is normalized to whole-machine percent (Task-Manager
style); System Idle Process (pid 0) is excluded — it measures idleness and
poisons "wait until quiet" predicates. Fixed bounded sampling windows.

### hub/notify.py — desktop notifications (write-only)

```
python hub\notify.py send --title T --message M [--duration short|long] [--silent]
```

Notes: native Windows toasts (winotify/WinRT) under app id "Remote Hub".
Title/message are control-char-stripped and clamped (64/512 chars, honest
`truncated` flags); the show call is deadline-bounded (10s) so a wedged
toast pipeline cannot hang the hub.

### hub/push.py — phone push notifications (ntfy)

```
python hub\push.py configure --topic T [--server S]
python hub\push.py status                     is a topic set? (masked)
python hub\push.py send --title T --message M [--priority ...] [--tags ...]
python hub\push.py test
```

Notes: pushes to the phone's ntfy app via ntfy.sh (or a self-hosted server).
The topic is the shared secret, kept OUT of git — stored in gitignored
`hub/.push.json` (written by `configure`) or via `HUB_NTFY_TOPIC`. Fail-soft /
inert until configured, so `friday respond` calls it safely whether or not it's
set up. The HTTP POST is timeout-bounded; header values are ASCII-sanitized.
`friday respond` fires this (priority `urgent`) alongside the desktop toast, so
a real security signal reaches your phone even when you're away from the PC.

### hub/mail.py — IMAP/SMTP mail (imaplib + smtplib)

Multi-provider: `--provider icloud|gmail` (icloud default) on every subcommand.

```
python hub\mail.py check     [--provider P]
python hub\mail.py count     [--provider P]   # quiet unread count, no headers
python hub\mail.py mailboxes [--provider P]
python hub\mail.py unread    [--provider P] [--limit N]
python hub\mail.py read      [--provider P] --uid U [--mailbox M] [--max-chars N]
python hub\mail.py send      [--provider P] --to A --subject S --body B [--from F] [--confirm-send]
```

Notes: per-provider host/user/cred mapping lives in `PROVIDERS`; the login user
and keyring account come from the gitignored local config
(`hub/.hub_local.json`, see `hub_local.example.json`) so no identity is in the
repo. Overridable per call via `--user`/`--cred-service`/`--cred-account` or
`HUB_MAIL_*` env.
Password pulled in-process from keyring, never printed.
Reads open the mailbox **readonly** and fetch with BODY.PEEK — reading never
marks mail as seen; limits are hard-capped (unread ≤25, body ≤20k chars) so a
mailbox can't flood context. `send` is fail-closed: validates addresses
(header-injection-safe regex), caps subject/body, and only transmits with
`--confirm-send` — default is a dry-run preview. Kill-switch is asserted
before every IMAP/SMTP handshake; all sockets carry a 20s timeout.
Orchestration rule: a real send only transmits with `--confirm`; when Claude is
driving, it confirms the recipient/subject/body with the user first.

### hub/friday.py — FRIDAY orchestrator (router + scenes)

```
python -m hub.friday boot    --profile dev  [--no-mail] [--dry-run]
python -m hub.friday trigger sit-down       [--dry-run]
python -m hub.friday trigger chill          [--dry-run]
python -m hub.friday trigger optimize       [--dry-run]
python -m hub.friday trigger goodnight      [--dry-run]   wind down + lock
python -m hub.friday status                 [--no-mail]   read-only health dashboard
python -m hub.friday respond                [--dry-run]   security tripwire handler
python -m hub.friday watch status                          is the tripwire registered?
python -m hub.friday watch install          [--confirm]   register the event-trigger task
python -m hub.friday watch uninstall        [--confirm]   remove it
```

**watch** manages the Windows Scheduled Task whose EVENT TRIGGER launches
`respond`. The task fires on Security event 4625 (failed logon) and Defender
Operational 1116/1117 (malware detected/acted), runs hidden + single-instance
(`IgnoreNew`, no pile-up) in the user session at highest privilege (the toast
needs the desktop; reading the Security log needs elevation). `watch status` is
read-only; `watch install`/`uninstall` are dry-run previews by default (install
prints the full task XML + the `schtasks` command) and only act with `--confirm`
in an elevated terminal — creating a Security-log-triggered task needs admin, so
this final registration step is **NEEDS-YOU**.

The **respond** handler is the 5b autonomous tripwire — the stateless process
the OS launches on a Windows security event (see below). Fail-soft, bounded, and
NEVER mutates: kill-switch gate (a disarmed hub acts on nothing) → cooldown
marker (`hub/.respond_cooldown`, gitignored; skips if it alerted < `cooldown_sec`
ago — anti-storm) → read-only `security intruders` → compare to the
`security_watch` threshold → emergency `notify` toast if crossed. The threshold
is DATA in `profiles.json`:

```json
"security_watch": { "failed_logon_count": 10, "window_sec": 3600,
                    "cooldown_sec": 300, "defender_any": true }
```

`--dry-run` evaluates and reports what it *would* alert on, writing no marker and
sending no toast. Registering the OS event-trigger that launches `respond`
(`watch install`) needs admin and is the next step (NEEDS-YOU).

The **goodnight** scene safe-wipes windows (graceful-only, dev env protected),
turns lights + RGB off, then locks the session. Its final power-down runs
through a scene `power` step that accepts only REVERSIBLE verbs
(`lock`/`monitor-off`/`sleep`/`hibernate`) — shutdown/restart are refused in a
scene because friday auto-passes `--confirm`, so a data-only profile must never
be able to silently confirm an irreversible power-off. `--dry-run` plans every
step (including the lock) and executes nothing.

The **status** scene is a read-only, one-shot dashboard: it composes the
read-only module CLIs (safety kill-status + system snapshot + net status +
mail counts + a non-aborting vault audit) into a single envelope. Because it
never acts, it REPORTS the kill-switch state rather than obeying it — the one
scene you want working even when the hub is disarmed. Exit 0 clean, 2 if a
read failed, never 1.

Composes the tested module CLIs into deterministic routines — not a sensor,
not LLM logic. Profiles are DATA in `hub/profiles.json`; editing one never
touches code. Profile keys: `requires` (offline vault preconditions),
`launch` (`{type: code|app|url}`), `wipe` (`{protect: [...]}`), `smart_home`
(`{wemo: {device, action}}`).

`boot` pipeline (fail-closed, ordered): kill-switch gate → offline vault audit
(missing *required* cred aborts before any launch) → quiet unread counts per
provider (fail-soft, `--no-mail` skips) → launch each entry.

`trigger` pipeline: gate → vault → **safe wipe** → launch → smart-home.

**Safe wipe** graceful-closes visible top-level windows via WM_CLOSE only —
**never `--force`**, so a window with unsaved changes survives and is reported
as kept, never killed. Two protection layers exempt a window: the profile's
`protect` patterns (matched against process name AND title) plus an always-on
`SELF_PROTECT` list (Claude, VS Code, the shell/terminal, python, Windows
shell). A window closes only if it matches neither. A scene protects what it
launches (e.g. `sit-down` protects `fortnite`/`epic`) so its own wipe can't
kill it.

**Smart-home** is a local UPnP layer via `pywemo` (SSDP discovery + toggle),
lazy-imported and fail-soft — inert until `pip install pywemo`, then `chill`
discovers and toggles the named Wemo switch on the LAN.

**optimize** is a max-cleanup scene: a read-only top-consumers `report`
(RAM/CPU via system.py) then an **aggressive window wipe** (`protect: []`, so
only `SELF_PROTECT` survives). It is deliberately **window-scoped, not
process-table-scoped** — killing every process outside a small allowlist would
terminate `svchost`/`lsass`/drivers/AV and crash Windows without freeing
reclaimable RAM, so FRIDAY never does that. A profile may also carry a
`kill: [names]` list for windowless tray/background apps (e.g. Discord, Epic,
Steam); each goes through the guarded `apps kill`, which is **fail-closed**:
refuses `CRITICAL_KILL_GUARD` names, refuses if the invoking user can't be
resolved, and targets a process only if its owner is positively the invoking
user (unreadable/elevated/SYSTEM owners are skipped, never killed).

**RGB** (`rgb: green|red|purple|orange|<hex>`) sets Alienware zones via an
AlienFX/AWCC controller. AWCC exposes no stable public color CLI, so this is a
fail-soft scaffold: it resolves a controller (`HUB_ALIENFX_CLI`), runs a
bounded call with an overridable arg template (`HUB_ALIENFX_ARGS`), and reports
**inert** if none is found — never fatal, never hangs. Runs last so lighting
matches the active scene.

stdout = JSON envelope; stderr = live `[FRIDAY]` narration.
Exit: 0 clean · 1 aborted · 2 degraded. `--dry-run` shows the full plan
(including which windows the wipe would close vs protect) without acting.

### hub/window.py — window layout control

```
python hub\window.py list                                read-only: windows + geometry
python hub\window.py move   <target> --x X --y Y [--width W --height H]
python hub\window.py resize <target> --width W --height H
python hub\window.py snap   <target> --to PRESET
   target  = --hwnd H | --title S | --pid N   (ambiguity = error w/ candidates)
   PRESET  = left right top bottom | top-left top-right bottom-left bottom-right
             | maximize minimize restore center full
```

Notes: reuses apps.py's window enumerator; resolves to exactly one window
(ambiguity is a structured error, never a guess) then acts directly — same
posture as apps focus/close (a reposition is benign/reversible). Snap presets
are computed from the window's monitor work area (taskbar excluded) via
GetMonitorInfo, so halves/quadrants land right on multi-monitor setups; a
maximized window is restored before positioning. pywin32 only, each op a single
bounded Win32 call.

### hub/net.py — read-only network telemetry

```
python hub\net.py status                      hostname, primary IP, online probe
python hub\net.py adapters                     per-NIC up/speed/IPv4/IPv6/MAC
python hub\net.py wifi                          SSID / signal / state (or absent)
python hub\net.py ping --host H [--count N]     bounded reachability + loss/latency
```

Notes: dependency-free (psutil + socket + netsh/ping). Read-only — never
changes network config. `status`'s online check is a bounded TCP connect to a
public DNS port (no data sent). `ping` validates the host against a safe
charset (no leading dash → no arg injection), caps `--count`, and carries
per-reply + overall timeouts; the summary parse is anchored on the `Packets:`
line so `bytes=`/`TTL=` numbers are never mistaken for counts (English-locale
best-effort, honest `null` otherwise — reachability comes from the exit code).

### hub/power.py — power & session control (confirm-gated)

```
python hub\power.py status                          read-only: AC/battery + scheme
python hub\power.py lock         [--confirm]
python hub\power.py monitor-off  [--confirm]
python hub\power.py sleep        [--confirm]
python hub\power.py hibernate    [--confirm]
python hub\power.py shutdown     [--confirm] [--delay N]
python hub\power.py restart      [--confirm] [--delay N]
python hub\power.py cancel                          abort a pending shutdown
```

Notes: dependency-free (ctypes → user32/powrprof/kernel32 + shutdown.exe).
Every *acting* verb is a dry-run PREVIEW by default and only fires with
`--confirm` (mirrors mail's fail-closed send), so a disruptive action never
fires without it (and when Claude is driving, it confirms with the user first).
`status` is read-only; `cancel` is an
always-safe undo. Effects are bounded — SendMessageTimeout blanks the display
without hanging, shutdown.exe carries a timeout, and `shutdown`/`restart`
default to a 60s delay so `cancel` has a window. Orchestration rule: a
`--confirm` power action requires that explicit flag; when Claude is driving,
it confirms with the user first.

### hub/media.py — media transport & volume keys

```
python hub\media.py keys                        read-only: supported keys + codes
python hub\media.py play-pause | next | prev | stop
python hub\media.py mute
python hub\media.py volume-up   [--steps N]     N capped at 50
python hub\media.py volume-down [--steps N]
```

Notes: dependency-free (ctypes → user32.keybd_event); the shell routes the
media VKs to the active media session, so no target window is needed. These
act DIRECTLY (no --confirm) by deliberate exemption: a media key is benign and
instantly reversible (no data loss, no outward effect), unlike the gated
power/mail mutations. `--steps` is capped so a stuck request can't runaway.

### hub/clipboard.py — clipboard access (secret-aware)

```
python hub\clipboard.py get   [--max-chars N] [--reveal]
python hub\clipboard.py set   --text T [--confirm]
python hub\clipboard.py clear [--confirm]
```

Notes: `get` is read-only and length-capped (honest `truncated`); `set`/`clear`
clobber the clipboard so they are confirm-gated (dry-run preview by default).
Secret hygiene: `get` runs a conservative heuristic (PEM key blocks, secret
keywords, or a single opaque high-entropy token) and WITHHOLDS matching content
(`content:null`, `looks_sensitive:true`) unless `--reveal` — so a hub read never
dumps a copied password into its output. Clipboard opens are bounded (deadline+retry)
so a momentarily-locked clipboard can't hang the hub. Uses win32clipboard.

### hub/screen.py — screen capture (staged, dependency-free PNG)

```
python hub\screen.py displays                    read-only: monitors + geometry
python hub\screen.py capture [--display N | --all]   default --all
```

Notes: `capture` BitBlts the screen (whole virtual desktop or one monitor),
encodes a PNG by hand with stdlib zlib (no image library), and writes it into
an allowed files.py root (Pictures/RemoteHub by preference) with a timestamped,
uniquified name — never overwriting. Returns the staged path + sha256; image
bytes never touch stdout. The orchestrator then sends the file via files.py's
staging channel. BGRX→RGBA is a C-level slice swap (no per-pixel Python loop),
so even a 2560px grab encodes fast. pywin32 + stdlib only.

### hub/security.py — PC security posture audit (read-only)

```
python hub\security.py audit                        read-only posture scorecard + concerns
python hub\security.py intruders [--hours N] [--max N]   read-only recent-signal report
python hub\security.py scan [--full]    [--confirm]      Defender scan (detached)
python hub\security.py firewall-on      [--confirm]      enable firewall (all profiles)
python hub\security.py realtime-on      [--confirm]      enable Defender real-time
python hub\security.py disable-smb1     [--confirm]      disable legacy SMBv1
python hub\security.py update-sigs      [--confirm]      update Defender signatures
```

**Hardening verbs** are dry-run previews by default and only act with
`--confirm`; acting also REQUIRES an elevated terminal (they refuse fail-closed
otherwise). By construction there is **no verb that weakens a protection** —
only enable/scan/update; there is deliberately no realtime-off or firewall-off.
Each maps to one bounded PowerShell cmdlet; `scan` launches detached so a long
full scan never blocks the hub. Orchestration rule: a real `--confirm` hardening
action is confirmed with the user (and run in an admin terminal) — see NEEDS-YOU.

**intruders** is the read-only recent-signal report: failed logons (Security
event 4625 in the last `--hours`, needs admin → fail-soft `available:false` +
a note when unelevated, never an error), recent Defender threat detections, and
current local admins. Counts + a bounded list; `concerns` flags only hard
signals (a Defender detection). Count-based thresholds ("too many 4625") live in
the brain / the 5b tripwire's data profile, never in the module.

Notes: one bounded PowerShell gather assembles Defender (real-time/AV/tamper/
signature age), firewall profiles, UAC, RDP, SMBv1, listening TCP ports,
BitLocker, local admins, and the guest account into JSON — each check
try/catch'd so a missing cmdlet or unelevated run yields `null` for that field,
never sinking the report. Most checks work unelevated; BitLocker (and sometimes
local admins) need admin and come back null with a note. `concerns` flags only
hard on/off invariants (Defender off, a firewall profile off, UAC off, RDP on,
SMBv1 on, guest on) — universally-agreed issues, NOT tunable thresholds; numeric
values are reported raw so the orchestrator decides what's "too old". Mutating
hardening verbs (scan/firewall-on/…) are a later, admin-gated + --confirm step.

### hub/files.py — contained file access

```
python hub\files.py roots
python hub\files.py search --pattern GLOB [--root R] [--limit N] [--timeout S]
python hub\files.py info   --path P [--hash]
python hub\files.py read   --path P [--max-bytes N] [--tail]
python hub\files.py stage  --path P [--max-mb N]
```

Notes: `search` reports `complete`/`bounded_by` honestly (timeout, scan cap,
result limit). `stage` validates containment + size and returns sha256; the
operator then pulls the staged file (e.g. `scp` over the SSH channel, or
Claude's native file channel when it is driving).

## Driving the hub

Two modes, same modules:

- **You drive it** (direct terminal) — run the CLIs and read the JSON yourself.
  Predicate example, *"back up only if disk is safe"*: `hub system disk` →
  check `free_gb > 20` → proceed or stop. `hub help` lists every command.
- **Claude drives it** — launch `claude` in the terminal (or use the Claude
  app) and it chains module calls + browser actions (Chrome MCP), evaluates the
  JSON between steps, and reports a unified result. Canonical example (proven
  end-to-end): launch app → navigate Chrome to a login page → read page state →
  `credentials.py check` → **halt** the auth leg if `exists:false` (fail-closed
  credential gate) → graceful cleanup → one JSON summary.

Either way the modules are identical dumb hands; only the *driver* changes.

## Environment

- Windows 11, Python 3.14 (`pip install -r requirements.txt`: psutil, pywin32)
- Repo: this folder (any path); modules run as scripts
  (`python hub\apps.py …`) or package (`python -m hub.apps`) — imports handle
  both.
- Runtime state that must never be committed: `hub/KILLSWITCH` (gitignored).

## Extension rules

New modules must: (1) emit the standard envelope with `ensure_ascii=True`,
(2) call `assert_alive("module.action")` before acting, (3) bound every wait
with `Deadline`/`wait_until` and every loop with a ceiling, (4) route retries
through `@retry`, (5) refuse ambiguity with structured errors, (6) keep
secrets and predicate logic out of the module. Verify against the live
system before committing; selftests use temp state, never real switches.
