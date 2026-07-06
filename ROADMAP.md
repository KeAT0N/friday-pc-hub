# Remote Hub — Autonomous Improvement Roadmap

Living backlog for the self-paced improvement effort. Claude works top-down by
value, verifying each change against the live machine before committing. Updated
every iteration so any future session can resume mid-stream.

Legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[!]` needs the user

## Phase 1 — Foundation: a real test suite
The single biggest gap: 2,710 lines of hub code, zero external tests. Built on
stdlib `unittest` (no new deps, matches the project's self-contained ethos).
A single runner (`python -m tests.run`) executes everything and emits a summary.

- [x] tests/ scaffold + `tests.run` discovery runner
- [x] test_safety.py — kill-switch, Deadline, @deadline, @retry, wait_until (23 tests, green)
- [x] test_envelope.py — cross-module: valid ASCII envelope + exit code mirrors `ok` (10 modules)
- [x] test_system.py — telemetry shape, cpu percent = per-core mean, pid-0 exclusion, top normalized whole-machine (9 tests)
- [x] test_files.py — containment allowlist, `..`/outside escape, refused dot-dirs, key-material refusal, binary + bounded/truncated read (19 tests)
- [x] test_credentials.py — Secret wrapper per-channel, provider registry, format validation, fail-closed enroll (26 tests). FOUND+FIXED a real bug: Windows `isatty()` returns True for NUL, so the non-TTY enroll gate hung on getpass instead of refusing — hardened with a GetConsoleMode probe.
- [ ] test_apps.py — ambiguity errors, guarded kill fail-closed (mock-based)
- [ ] test_friday.py — dry-run planning, protect-layer logic, exit codes

## Phase 2 — Breadth: new capable modules
Each follows the extension rules (envelope, assert_alive, bounded, fail-closed).

- [ ] window.py — enumerate/move/resize/snap windows, virtual-desktop switch, layout presets
- [ ] audio.py — master volume/mute, default device switch, per-app volume
- [ ] power.py — lock/sleep/hibernate/monitor-off/scheduled-shutdown (all guarded, confirm-gated)
- [ ] clipboard.py — read/write clipboard, bounded, key-material sniffing
- [ ] media.py — playback control via media keys (play/pause/next/prev/vol)
- [ ] net.py — wifi status, connectivity/ping, adapter list, bounded speed probe
- [ ] screen.py — screenshot to staged file (goes through files.py staging)

## Phase 3 — Depth: richer FRIDAY orchestration
- [ ] `status` scene — one-shot health dashboard (system + mail + vault audit)
- [ ] scene composition: new profiles leveraging window/audio/power modules
- [ ] `goodnight` scene — wipe, lock, monitors off, lights off
- [ ] per-scene RGB + audio ducking integration

## Phase 4 — Hardening
- [ ] envelope contract fuzz (control chars, unicode, oversized fields)
- [ ] kill-switch coverage audit across every new module
- [ ] deadline/loop-ceiling audit
- [ ] README kept in lockstep with every module added

## Phase 5 — PC security hardening (`hub/security.py`) [user-requested]
"Make the PC super safe from viruses and intruders." Same hub philosophy:
a read-only security *sensor* first (dumb hands, brain scores it), then
guarded, confirm-gated, admin-aware hardening actions that NEVER weaken
protection and are reversible where possible.

- [ ] security.py `audit` — read-only posture scorecard, one envelope:
      Defender (real-time on? sigs current? last scan) via Get-MpComputerStatus;
      firewall profile states (Get-NetFirewallProfile); pending Windows Updates;
      BitLocker status; UAC level; RDP + SMBv1 enabled?; listening TCP ports
      (Get-NetTCPConnection -State Listen); enabled local admins + guest acct.
      Each normalized to bool/number so the brain can flag issues.
- [ ] security.py `intruders` — read-only recent-signal report: failed logons
      (Event ID 4625), new/changed local admins, listening ports vs a saved
      baseline, recently added scheduled tasks / Run-key autoruns.
- [ ] security.py hardening verbs (each --confirm gated, refuses w/o admin,
      says what it changed): `scan` (Defender quick/full), `update-sigs`,
      `firewall-on`, `realtime-on`, `disable-smb1`, `uac-on`. Fail-closed;
      never disables a protection; dry-run preview by default.
- [ ] test_security.py — audit parsing on captured sample output; confirm-gate
      + admin-gate refusals; every verb dry-runs without acting.
- [ ] FRIDAY `lockdown` scene composing audit + firewall-on + realtime-on + scan
- [ ] README: new Module CLIs section for security.py

## NEEDS-YOU (blocked on the user — building around these)
- [ ] `pip install pywemo` to activate real Wemo smart-home discovery
- [ ] Enroll real credentials at the PC terminal (iCloud/Gmail app-passwords) when ready
- [ ] Approve any real email send in chat before it transmits
- [ ] Confirm physical devices (lights/Wemo) when smart-home scenes are tested
- [ ] Phase 5: run an ELEVATED (admin) terminal for the actual hardening verbs
  (scan/firewall-on/etc.) — the read-only `audit`/`intruders` reports mostly
  work unelevated and get built first; the mutating verbs need your admin OK.

## Log
- 2026-07-05: Kicked off autonomous effort. Baseline healthy (Py 3.14, all
  selftests green). Chose unittest over pytest to avoid new deps. Roadmap created.
- 2026-07-05: test_safety (23) + test_envelope (cross-module) committed. 26 green.
- 2026-07-05: test_files (19) — containment boundary. Confirmed deny-dir logic
  by accident: a temp-rooted sandbox is blocked because system temp is under
  AppData (a refused dir); sandbox relocated under home. Suite at 45 green.
- 2026-07-05: test_credentials (26). Tests surfaced a real fail-closed bug:
  Windows `sys.stdin.isatty()` reports True for the NUL device, so `enroll`
  via an orchestration channel hit getpass and HUNG (30s) instead of refusing.
  Hardened `_stdin_is_interactive()` with a GetConsoleMode probe (fails for
  NUL/redirected, succeeds for a real console). Enroll now refuses in ~0.5s.
  Also hardened tests/_helpers run_cli to use DEVNULL stdin. Suite at 71 green.
- 2026-07-05: test_system (9) — telemetry invariants, pid-0 exclusion, whole-
  machine cpu normalization verified against the live box. Suite at 80 green.
  Phase 1 remaining: test_apps, test_friday (both need light mocking).
- 2026-07-05: User requested Phase 5 — make the PC "super safe from viruses and
  intruders." Added as a read-only-audit-first security module + guarded
  hardening verbs + a FRIDAY lockdown scene. Loop stop-condition extended to
  cover all phases (was stopping at Phase 4).
