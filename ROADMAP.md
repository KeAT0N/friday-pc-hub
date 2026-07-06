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
- [ ] test_system.py — telemetry shape, percent normalization, pid-0 exclusion
- [ ] test_files.py — containment allowlist, symlink/`..` escape, key-material refusal, bounded read
- [ ] test_credentials.py — Secret wrapper seals all 8 channels, existence-only CLI
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

## NEEDS-YOU (blocked on the user — building around these)
- [ ] `pip install pywemo` to activate real Wemo smart-home discovery
- [ ] Enroll real credentials at the PC terminal (iCloud/Gmail app-passwords) when ready
- [ ] Approve any real email send in chat before it transmits
- [ ] Confirm physical devices (lights/Wemo) when smart-home scenes are tested

## Log
- 2026-07-05: Kicked off autonomous effort. Baseline healthy (Py 3.14, all
  selftests green). Chose unittest over pytest to avoid new deps. Roadmap created.
