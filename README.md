# Remote Hub

Phone → Claude → PC control system. The user issues requests from the Claude
app on their phone; Claude (the orchestrating session on this Windows 11 PC)
is the **brain**, and the Python modules in `hub/` are deliberately **dumb,
reliable hands**. There is no daemon, no custom RPC layer, no LLM logic in the
scripts — the Claude session itself is the runtime, which collapses most of
the attack surface by construction.

This README is the execution-model contract. A future Claude session that
reads it should operate the hub identically to the one that built it.

```
┌─────────┐   Claude app    ┌──────────────────┐
│  Phone   │ ──────────────▶ │  Claude session   │  ← brain: plans, parses JSON,
└─────────┘                 │  (this repo cwd)  │    evaluates predicates, decides
                            └───┬───────┬──────┘
                    PowerShell/Bash     │ native Chrome MCP
                                ▼       ▼
                        ┌──────────┐  ┌─────────────┐
                        │  hub/*.py │  │ Chrome tabs  │  ← browser leg has NO local
                        │  modules  │  │ (extension)  │    module on purpose
                        └──────────┘  └─────────────┘
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
5. **Browser = native extension, not scripts.** Navigation, reading, clicking
   happen through Claude's Chrome MCP tools live. Selenium-style automation is
   explicitly out of scope.
6. **`ensure_ascii=True` everywhere.** JSON envelopes are pure ASCII
   (`\uXXXX` escapes) so cp1252 consoles can never corrupt them.

## Structural security boundaries

These are enforced by construction, not convention:

- **Secrets never transit chat.** `credentials.py` exposes secrets only
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
python hub\apps.py query  (--pid N | --name S | --title S)
python hub\apps.py launch --target <exe|path|URI> [--args ...]
                          [--wait-title S] [--timeout N]
python hub\apps.py focus  (--title S | --pid N | --hwnd N) [--timeout N]
python hub\apps.py close  (--title S | --pid N | --hwnd N) [--timeout N] [--force]
```

Notes: launcher pid often ≠ app pid (UWP brokers) — `--wait-title` is the
reliable readiness signal. `close` is graceful-first (`WM_CLOSE`); if the
window survives (unsaved-changes dialog), it refuses unless rerun with
`--force`. Ambiguous matches error out listing candidates with hwnds.

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

**Enrollment is local-terminal-only by construction.** `enroll` demands an
interactive terminal and reads the secret via hidden getpass prompt; run
through an orchestration channel (non-TTY stdin) it refuses and prints the
command to run at the PC instead. The terminal check is hardened for Windows,
where `isatty()` wrongly reports the NUL device as a TTY — a `GetConsoleMode`
probe confirms a real console so the gate refuses (never hangs on getpass)
when driven headless. Secrets therefore never appear in chat logs, argv,
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

Notes: per-provider host/user/cred mapping lives in `PROVIDERS`
(icloud→`icloud_mail`, gmail→`gmail`; both account `keatondavey`), overridable
per call via `--user`/`--cred-service`/`--cred-account` or `HUB_MAIL_*` env.
Password pulled in-process from keyring, never printed.
Reads open the mailbox **readonly** and fetch with BODY.PEEK — reading never
marks mail as seen; limits are hard-capped (unread ≤25, body ≤20k chars) so a
mailbox can't flood context. `send` is fail-closed: validates addresses
(header-injection-safe regex), caps subject/body, and only transmits with
`--confirm-send` — default is a dry-run preview. Kill-switch is asserted
before every IMAP/SMTP handshake; all sockets carry a 20s timeout.
Orchestration rule: an actual send is confirmed with the user in chat first.

### hub/friday.py — FRIDAY orchestrator (router + scenes)

```
python -m hub.friday boot    --profile dev  [--no-mail] [--dry-run]
python -m hub.friday trigger sit-down       [--dry-run]
python -m hub.friday trigger chill          [--dry-run]
python -m hub.friday trigger optimize       [--dry-run]
```

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
`--confirm` (mirrors mail's fail-closed send), so the orchestrator confirms
disruptive actions in chat first. `status` is read-only; `cancel` is an
always-safe undo. Effects are bounded — SendMessageTimeout blanks the display
without hanging, shutdown.exe carries a timeout, and `shutdown`/`restart`
default to a 60s delay so `cancel` has a window. Orchestration rule: an actual
`--confirm` power action is confirmed with the user in chat first.

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
orchestrator then sends the file to the phone via its native file channel.

## Orchestration pattern

A workflow is: Claude chains module calls + browser actions, evaluates the
JSON between steps, and reports a unified result. Canonical example (proven
end-to-end): launch app → navigate Chrome to a login page → read page state →
`credentials.py check` → **halt** the auth leg if `exists:false` (fail-closed
credential gate) → graceful cleanup → unified JSON report to chat.

Predicate example: *"back up only if disk is safe"* →
`python hub\system.py disk` → orchestrator checks `free_gb > 20` → proceed
or halt.

## Environment

- Windows 11, Python 3.14 (`pip install -r requirements.txt`: psutil, pywin32)
- Repo: `C:\Users\coold\Desktop\Claude`; modules run as scripts
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
