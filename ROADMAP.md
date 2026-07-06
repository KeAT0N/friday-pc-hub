# Remote Hub — Autonomous Improvement Roadmap

Living backlog for the self-paced improvement effort. Claude works top-down by
value, verifying each change against the live machine before committing. Updated
every iteration so any future session can resume mid-stream.

Legend: `[ ]` todo · `[~]` in progress · `[x]` done · `[!]` needs the user

## Phase 1 — Foundation: a real test suite  ✅ COMPLETE (109 tests green)
The single biggest gap: 2,710 lines of hub code, zero external tests. Built on
stdlib `unittest` (no new deps, matches the project's self-contained ethos).
A single runner (`python -m tests.run`) executes everything and emits a summary.

- [x] tests/ scaffold + `tests.run` discovery runner
- [x] test_safety.py — kill-switch, Deadline, @deadline, @retry, wait_until (23 tests, green)
- [x] test_envelope.py — cross-module: valid ASCII envelope + exit code mirrors `ok` (10 modules)
- [x] test_system.py — telemetry shape, cpu percent = per-core mean, pid-0 exclusion, top normalized whole-machine (9 tests)
- [x] test_files.py — containment allowlist, `..`/outside escape, refused dot-dirs, key-material refusal, binary + bounded/truncated read (19 tests)
- [x] test_credentials.py — Secret wrapper per-channel, provider registry, format validation, fail-closed enroll (26 tests). FOUND+FIXED a real bug: Windows `isatty()` returns True for NUL, so the non-TTY enroll gate hung on getpass instead of refusing — hardened with a GetConsoleMode probe.
- [x] test_apps.py — guarded-kill owner scoping (same-user-only, identity-required, guard-before-scan), resolve ambiguity refusal, kill-switch refusal (12 tests, psutil mocked)
- [x] test_friday.py — safe-wipe protect layers (SELF_PROTECT + pattern name/title, unsaved kept-not-killed), kill-skip guard, RGB/smart-home dry-run, run_module resilience, exit codes 0/1 (17 tests, run_module mocked)

## Phase 2 — Breadth: new capable modules
Each follows the extension rules (envelope, assert_alive, bounded, fail-closed).

- [x] window.py — list (RO) + move/resize/snap; 12 snap presets from monitor work area; resolves one window (ambiguity=error) then acts like apps focus/close; pure snap-math unit-tested (13 tests). (virtual-desktop switch deferred — undocumented COM; not worth the fragility)
- [x] audio.py — DONE-BY-MEDIA: volume up/down/mute already covered dependency-free by media.py's media keys. Absolute get/set + per-app + default-device switch need pycaw (new dep) → deferred to NEEDS-YOU; not worth a new dep for marginal gain now.
- [x] power.py — status (RO) + lock/monitor-off/sleep/hibernate/shutdown/restart/cancel, all confirm-gated dry-run-by-default, ctypes-only, bounded (7 tests)
- [x] clipboard.py — get (read-only, secret-withholding heuristic + --reveal, length-capped) + confirm-gated set/clear, bounded clipboard opens (12 tests)
- [x] media.py — play-pause/next/prev/stop/mute/volume-up/down via ctypes keybd_event, read-only `keys`, --steps capped; media keys act directly (benign, documented exemption) (7 tests)
- [x] net.py — status (hostname/IP/online), adapters (up/speed/IPv4/6/MAC), wifi (netsh), bounded ping w/ host-injection guard + anchored parse (10 tests). Found+fixed a ping-parse bug (bytes=/TTL= misread as counts).
- [x] screen.py — displays (RO monitor enum) + capture; hand-rolled PNG via stdlib zlib (no image dep), writes into an allowed files.py root, staged path + sha256, never overwrites (8 tests)

**Phase 2 COMPLETE ✅ — 166 tests green. New capability modules: power, net,
clipboard, media, window, screen (+ audio covered by media).**

## Phase 3 — Depth: richer FRIDAY orchestration
- [x] `status` scene — read-only health dashboard: kill-switch state + system snapshot + net status + mail counts + non-aborting vault audit, one envelope, exit 0/2 (5 tests). Reports the kill-switch instead of obeying it.
- [x] `goodnight` scene — safe-wipe + lights off + RGB off + lock, via a data profile + new reversible-only scene `power` step (shutdown/restart refused in scenes); fully dry-run-able (5 tests)
- [~] scene composition (window/audio/power) — DEFERRED: the `power` step already composes power.py in goodnight; window-layout-in-scene is niche (do ad-hoc via window.py); RGB already per-scene. Not worth a bespoke step.
- [~] per-scene RGB + audio ducking — DEFERRED: RGB is already per-scene (rgb key). Audio ducking needs pycaw (NEEDS-YOU); media keys can't set an absolute duck level. Revisit if pycaw lands.

**Phase 3 substantially COMPLETE ✅ — status + goodnight scenes shipped; the two
remaining items deferred with rationale (low value / need pycaw).**

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

- [x] security.py `audit` — read-only posture scorecard: Defender (realtime/AV/
      antispyware/tamper/signature age/quick-scan age), firewall profiles, UAC,
      RDP, SMBv1, listening TCP ports, BitLocker, local admins, guest. One
      bounded PowerShell gather, per-check try/catch (null on fail), works
      unelevated. `concerns` flags hard on/off invariants only (no thresholds).
      (8 tests; concern/notes logic unit-tested on samples + live smoke.)
- [x] security.py `intruders` — read-only recent-signal report: failed logons
      (Security 4625, --hours window, admin-gated → fail-soft available:false +
      note when unelevated), recent Defender threat detections, current local
      admins. Bounded counts+list; concerns = hard signals only (thresholds live
      in the brain / 5b profile). (8 tests: concern/notes on samples + live smoke.)
- [ ] security.py hardening verbs (each --confirm gated, refuses w/o admin,
      says what it changed): `scan` (Defender quick/full), `update-sigs`,
      `firewall-on`, `realtime-on`, `disable-smb1`, `uac-on`. Fail-closed;
      never disables a protection; dry-run preview by default.
### Phase 5b — Autonomous push tripwire (event-driven, NO daemon) [user-requested]
Windows Task Scheduler is the push pump: it natively triggers a task on a
Security-log event and launches our short-lived handler — so there is no
long-lived listener process to secure (stays on the hub's no-daemon
philosophy). Depends on the security.py read-only scanner above (built first).

- [ ] profiles.json `security_watch` DATA block (no eval/DSL): `failed_logon_count`,
      `window_sec`, `cooldown_sec`, `defender_any` (bool). Threshold is data the
      brain/OS compares — no predicate logic in the hands.
- [ ] friday.py `respond` — the tripwire handler the OS launches on an event.
      Pipeline (fail-soft, fully @deadline-bounded): kill-switch gate → cooldown
      marker check (skip if fired < cooldown_sec ago; anti-storm) → bounded
      read-only scan (security.py intruders/audit) → compare counts to
      `security_watch` threshold → if crossed, emergency notify.py toast
      (existing envelope). Never mutates; degraded envelope on any error.
- [ ] friday.py `watch install|status|uninstall` — register/inspect/remove the
      Scheduled Task. Trigger = Security EventID 4625 (failed logon) + Defender
      Operational 1116/1117 (malware detected/acted); action = `python -m
      hub.friday respond`. XML generated from config; task set Hidden +
      MultipleInstancesPolicy=IgnoreNew (no pile-up) + runs in the user session
      (so the toast reaches the desktop). Dry-run prints XML + schtasks command;
      real register needs admin (--confirm) -> NEEDS-YOU.
- [ ] test: respond threshold + cooldown + fail-soft on unreadable Security log
      (mocked); watch install dry-run XML/query shape; kill-switch refusal.
- [ ] test_security.py — audit parsing on captured sample output; confirm-gate
      + admin-gate refusals; every verb dry-runs without acting.
- [ ] FRIDAY `lockdown` scene composing audit + firewall-on + realtime-on + scan
- [ ] README: new Module CLIs section for security.py + the watch/respond tripwire

## NEEDS-YOU (blocked on the user — building around these)
- [ ] `pip install pywemo` to activate real Wemo smart-home discovery
- [ ] `pip install pycaw` for absolute audio volume get/set, per-app volume,
  and default-device switching (media.py already does up/down/mute dep-free)
- [ ] Enroll real credentials at the PC terminal (iCloud/Gmail app-passwords) when ready
- [ ] Approve any real email send in chat before it transmits
- [ ] Confirm physical devices (lights/Wemo) when smart-home scenes are tested
- [ ] Phase 5: run an ELEVATED (admin) terminal for the actual hardening verbs
  (scan/firewall-on/etc.) — the read-only `audit`/`intruders` reports mostly
  work unelevated and get built first; the mutating verbs need your admin OK.
- [ ] Phase 5b: run `friday watch install --confirm` in an ELEVATED terminal to
  register the event-triggered Scheduled Task (creating a Security-log trigger
  and reading that log both require admin). Everything up to that is built and
  dry-run-verified without elevation.

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
- 2026-07-05: test_apps (12) — mocked psutil to prove do_kill fail-closed owner
  scoping (skips other-user/None-owner/wrong-name, refuses w/o identity, guard
  trips before any scan) + resolve_one_window ambiguity refusal. Suite 92 green.
  Phase 1 complete except test_friday.
- 2026-07-05: User requested an autonomous PUSH tripwire — added as Phase 5b.
  Design: Windows Task Scheduler event trigger (Security 4625 + Defender
  1116/1117) launches a stateless `friday respond` handler — no daemon. Handler
  is kill-switch-gated, cooldown-debounced, deadline-bounded, fail-soft, and
  fires a notify.py emergency toast when a data-driven threshold is crossed.
  Depends on the security.py scanner (built first); task registration needs
  admin (queued in NEEDS-YOU).
- 2026-07-05: test_friday (17) — mocked run_module to prove the safe-wipe
  protection layers (SELF_PROTECT always kept, profile pattern matches process
  name OR title, unsaved windows reported kept not force-killed) and the
  kill-skip guard (SELF_PROTECT names never reach apps.kill). Plus RGB/smart-
  home dry-run planning, run_module resilience, and CLI exit codes 0/1.
  ** Phase 1 COMPLETE: 109 tests green across all 7 modules. ** Phase 2 next.
- 2026-07-05: Phase 2 module 1 — power.py. Dependency-free ctypes session
  control; every acting verb dry-run-by-default + --confirm (proven by mocking
  the OS-effect helpers, so tests never lock/sleep/shut-down the box). status
  read verified live (AC, Balanced scheme). Added to envelope contract. 116 green.
- 2026-07-05: Phase 2 module 2 — net.py (read-only). status/adapters/wifi/ping,
  dependency-free, host-injection guard on ping. Found+fixed a real parse bug:
  the loose regex read `bytes=32`/`TTL=128` as sent/received counts; re-anchored
  on the `Packets:` line + added a locale-independent regression test. 126 green.
- 2026-07-05: Phase 2 module 3 — clipboard.py. get read-only with a
  secret-withholding heuristic (never dumps a copied password to chat without
  --reveal); set/clear confirm-gated. Tests mock the io helpers so the live
  clipboard is never clobbered. 138 green.
- 2026-07-05: Phase 2 module 4 — media.py (ctypes keybd_event). Media/volume
  keys act directly (documented confirm-gate exemption: benign + instantly
  reversible). All acting verbs tested with _tap mocked (no real volume/
  playback change). Fixed a Py3.14 argparse gotcha (literal % in help string).
  145 green.
- 2026-07-05: Phase 2 module 5 — window.py. Snap/move/resize reusing apps
  enum_windows; 12 presets from the monitor work area. Pure snap-math tested
  directly; all mutating ops tested with Win32 helpers mocked (no real window
  moves). Virtual-desktop switching deferred (undocumented COM, too fragile for
  the hub's reliability bar). 158 green. Phase 2: 5/7 (screen, audio remain).
- 2026-07-05: Phase 2 module 6 — screen.py. GDI BitBlt capture + hand-rolled
  PNG encoder (stdlib zlib, no Pillow); fast BGRX->RGBA slice swap. Writes into
  an allowed files.py root, staged path + sha256, unique names. Encoder parsed
  back chunk-by-chunk in tests; capture pipeline tested with grab mocked (no
  real screenshots on disk). Verified a real 2560x1080 capture live. audio.py
  marked done-by-media (pycaw deferred). ** Phase 2 COMPLETE: 166 green. **
- 2026-07-05: Phase 3 scene 1 — friday `status`. Read-only dashboard composing
  safety/system/net/mail/credentials via run_module; non-aborting vault audit;
  reports (not obeys) the kill-switch. Live run: cpu/mem/disk/online/vault all
  read in ~5s. Composition tested with run_module mocked + a live smoke. 171 green.
- 2026-07-05: Phase 3 scene 2 — friday `goodnight`. Added a reversible-only
  scene `power` step to trigger() (lock/monitor-off/sleep/hibernate; shutdown/
  restart refused so a data profile can't silently auto-confirm an irreversible
  power-off). goodnight profile = wipe + lights off + rgb off + lock. Verified
  dry-run plans the lock without acting. 176 green.
- 2026-07-05: Phase 3 remainder deferred (rationale in-line); jumped to Phase 5.
  security.py `audit` shipped — read-only posture scorecard via one bounded
  PowerShell gather (Defender/firewall/UAC/RDP/SMB1/ports/BitLocker/admins/
  guest), per-check fail-soft, `concerns` = hard on/off invariants only.
  Live audit on this box: zero concerns (well-secured). 184 green.
- 2026-07-06: security.py `intruders` — read-only signal report (failed logons
  4625 admin-gated fail-soft, Defender detections, local admins). Live: no
  recent failed logons / detections. concern+notes logic unit-tested on samples;
  live smoke + hours-cap test. 192 green. Next: guarded hardening verbs, then 5b.
