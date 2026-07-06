# Bounded-loop / no-hang audit

The hub's no-hang guarantee: every wait has a deadline, every loop has a ceiling
or iterates an already-finite collection, and every long loop polls the
kill-switch. This file enumerates every loop / sleep site and its bound. A
companion test (`tests/test_bounds.py`) asserts the bounding constants still
exist and are sane, so a regression that removes a cap fails CI.

Legend: **Deadline** = monotonic time budget · **Cap** = fixed ceiling constant ·
**Finite** = iterates an already-bounded collection · **Kill** = polls the
kill-switch mid-loop.

## Blocking loops / retries / sleeps

| Site | Construct | Bound |
|------|-----------|-------|
| `safety.py` `wait_until` | `while True` | **Deadline**(timeout) + **Kill** each cycle; `dl.sleep(poll)` never oversleeps |
| `safety.py` `Deadline.sleep` | `time.sleep(min(want, remaining))` | never past the deadline |
| `safety.py` `retry` | `for attempt in range(1, attempts+1)` | attempt **Cap** + total-time **Deadline** + per-delay ceiling |
| `clipboard.py` `_open_bounded` | `while True` | **Deadline**(`OPEN_DEADLINE_SEC`=2s), raises on expiry |
| `files.py` `_sha256` | `while chunk := read(1MB)` | **Deadline**(`HASH_DEADLINE_SEC`=30s) checked per chunk |
| `files.py` `do_search` | nested `os.walk` | **Deadline**(timeout, cap `SEARCH_TIMEOUT_CAP`) + `SEARCH_SCAN_CAP` entries + `SEARCH_RESULT_CAP` + **Kill** every `KILL_CHECK_EVERY` |
| `system.py` `sec_top` / `sec_cpu` | `time.sleep(sample)` / `cpu_percent(interval=sample)` | `sample` clamped to `SAMPLE_MAX`=10s (`_clamp_sample`) — fixed in this audit |
| `screen.py` `_unique_path` | `while candidate.exists()` | `UNIQUE_CAP`=10k + pid fallback — fixed in this audit |
| `notify` / `power` / `security` / `friday` subprocess calls | — | every `subprocess.run` carries an explicit `timeout=` |

## Finite iteration (no ceiling needed)

- `apps.py` — `psutil.process_iter`, `enum_windows` (EnumWindows callback), kill
  targets, per-target windows: all finite OS enumerations.
- `system.py` — `disk_partitions`, `net_io_counters(pernic)`, `process_iter`.
- `net.py` — `net_if_addrs`, ping output `splitlines`.
- `mail.py` — mailbox list, chosen UIDs (hard-capped `--limit` ≤25), message parts.
- `security.py` — firewall profiles, hardening verb table, parsed rows.
- `screen.py` — scanline `range(height)`, `EnumDisplayMonitors`.
- `friday.py` — profile `launch`/`kill`/`protect` entries, `MAIL_PROVIDERS`,
  vault services, wipe candidates, schtasks LIST `splitlines`. Each module call
  goes through `run_module` with `timeout=MODULE_TIMEOUT`.
- `media.py` — `range(steps)` with `steps` clamped to `STEP_CAP`=50.

## Caller-set but always finite + kill-aware

`apps focus/close/kill` and `launch --wait-title` use `wait_until(..., timeout)`
where `timeout` is a CLI arg (default 10s). It is always a finite number and the
wait polls the kill-switch, so it cannot hang indefinitely and is interruptible.

## Verdict

No genuinely unbounded loop remains. Two theoretical/​input-driven cases were
tightened during this audit: `system --sample` is now clamped (`SAMPLE_MAX`), and
`screen._unique_path` now has a hard ceiling (`UNIQUE_CAP`).
