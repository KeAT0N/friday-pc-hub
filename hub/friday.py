"""
hub/friday.py — FRIDAY boot orchestrator (command router).

Composes the tested hub module CLIs into a deterministic boot routine. Not a
sensor and not an LLM: it reads a data-defined profile, verifies preconditions
fail-closed, then launches the profile's workspace.

    python -m hub.friday boot --profile dev [--no-mail] [--dry-run]

Pipeline (ordered, fail-closed):
  0. Gate      safety.assert_alive         kill-switch engaged  -> ABORT (1)
  1. Vault     credentials check (offline) required cred missing -> ABORT (1)
  2. Mail      mail count (SEARCH UNSEEN)  fail-soft per provider (never blocks)
  3. Launch    apps launch / code <dir>    per-item; missing target -> skip+report

Output contract:
  stdout = one JSON envelope {ok, action:"boot", data:{...}, error}
  stderr = live [FRIDAY] narration so you watch it spin up
Exit: 0 clean · 1 aborted (gate / missing required cred) · 2 booted-degraded.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

try:  # package import or direct script run
    from hub.safety import KillSwitchEngaged, assert_alive
except ImportError:
    from safety import KillSwitchEngaged, assert_alive

REPO = Path(__file__).resolve().parent.parent          # repo root for -m runs
PROFILES_PATH = Path(__file__).resolve().parent / "profiles.json"
VAULT_ACCOUNT = "keatondavey"                            # hub credential account
MAIL_PROVIDERS = ("gmail", "icloud")
MODULE_TIMEOUT = 45.0
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def log(msg: str) -> None:
    print(f"[FRIDAY] {msg}", file=sys.stderr, flush=True)


def emit(ok: bool, data=None, error: str | None = None, code: int = 0) -> None:
    print(json.dumps(
        {"ok": ok, "action": "boot", "data": data, "error": error},
        ensure_ascii=True,
    ))
    sys.exit(code)


def run_module(module: str, argv: list[str], timeout: float = MODULE_TIMEOUT) -> dict:
    """Run a hub module CLI and return its parsed JSON envelope (or a synthetic
    error envelope). Never raises — a failed sub-call is data, not a crash."""
    cmd = [sys.executable, "-m", f"hub.{module}", *argv]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd=str(REPO))
        lines = [ln for ln in p.stdout.splitlines() if ln.strip()]
        if not lines:
            return {"ok": False, "error": f"{module}: no output "
                    f"(stderr: {p.stderr.strip()[:200]})"}
        return json.loads(lines[-1])
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"{module}: timed out after {timeout}s"}
    except (json.JSONDecodeError, OSError) as e:
        return {"ok": False, "error": f"{module}: {type(e).__name__}: {e}"}


# ---------------------------------------------------------------- phases

def audit_vault(required: list[str]) -> dict:
    results = {}
    for svc in required:
        r = run_module("credentials", ["check", "--service", svc,
                                       "--account", VAULT_ACCOUNT,
                                       "--provider", "keyring"])
        results[svc] = bool(r.get("ok") and (r.get("data") or {}).get("exists"))
    return results


def mail_glance() -> dict:
    out = {}
    for prov in MAIL_PROVIDERS:
        r = run_module("mail", ["count", "--provider", prov])
        if r.get("ok"):
            d = r["data"]
            out[prov] = {"unread": d["unread"], "inbox_total": d["inbox_total"]}
        else:
            out[prov] = {"error": r.get("error", "unknown")}
    return out


def launch_entry(entry: dict) -> dict:
    t = entry.get("type")
    try:
        if t == "code":
            path = entry.get("path", "")
            if not os.path.isdir(path):
                return {"type": t, "label": path, "ok": False,
                        "error": "folder not found"}
            # `code` is a .cmd shim; route through cmd so it resolves on PATH.
            subprocess.Popen(["cmd", "/c", "code", path], cwd=str(REPO),
                             creationflags=CREATE_NO_WINDOW)
            return {"type": t, "label": path, "ok": True}
        if t in ("app", "url"):
            target = entry.get("target", "")
            argv = ["launch", "--target", target]
            if entry.get("args"):
                argv += ["--args", *entry["args"]]
            r = run_module("apps", argv)
            return {"type": t, "label": target, "ok": bool(r.get("ok")),
                    "error": None if r.get("ok") else r.get("error")}
        return {"type": t, "label": None, "ok": False,
                "error": f"unknown launch type {t!r}"}
    except Exception as e:
        return {"type": t, "label": entry.get("path") or entry.get("target"),
                "ok": False, "error": f"{type(e).__name__}: {e}"}


# ---------------------------------------------------------------- boot

def boot(args) -> None:
    t0 = time.monotonic()

    try:
        profiles = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        emit(False, error=f"cannot read profiles.json: {e}", code=1)
    profile = profiles.get(args.profile)
    if not profile:
        emit(False, error=f"unknown profile {args.profile!r}; "
             f"known: {sorted(profiles)}", code=1)

    # 0. gate
    try:
        assert_alive("friday.boot")
    except KillSwitchEngaged as e:
        log("gate BLOCKED - kill-switch engaged")
        emit(False, data={"profile": args.profile}, error=str(e), code=1)
    log("gate ok - kill-switch clear")

    # 1. vault (offline, fail-closed on required)
    required = profile.get("requires", [])
    vault = audit_vault(required)
    log("vault: " + "  ".join(
        f"{k} {'OK' if v else 'MISSING'}" for k, v in vault.items()))
    missing = [k for k, v in vault.items() if not v]
    if missing:
        log(f"ABORT - required credentials missing: {missing}")
        emit(False, data={"profile": args.profile, "preflight": vault},
             error=f"missing required credentials: {missing}", code=1)

    # 2. mail glance (fail-soft)
    mail = None
    if args.no_mail:
        log("mail: skipped (--no-mail)")
    else:
        mail = mail_glance()
        log("mail: " + " - ".join(
            f"{p} {i['unread']} unread" if "unread" in i else f"{p} ERR ({i['error']})"
            for p, i in mail.items()))

    # 3. launch
    launches = []
    entries = profile.get("launch", [])
    if args.dry_run:
        log("launch: DRY RUN - plan only")
        for e in entries:
            label = e.get("path") or e.get("target")
            log(f"  would launch: {e.get('type')} -> {label}")
            launches.append({"type": e.get("type"), "label": label,
                             "ok": None, "dry_run": True})
    else:
        for e in entries:
            res = launch_entry(e)
            log(f"launch: {res['type']} -> {res.get('label')} "
                + ("OK" if res["ok"] else f"FAIL ({res.get('error')})"))
            launches.append(res)

    elapsed = round(time.monotonic() - t0, 1)
    mail_fail = bool(mail) and any("error" in v for v in mail.values())
    launch_fail = any(l.get("ok") is False for l in launches)
    degraded = mail_fail or launch_fail
    log(f"booted profile '{args.profile}' in {elapsed}s"
        + (" (DEGRADED)" if degraded else ""))
    emit(not degraded,
         data={"profile": args.profile, "preflight": vault, "mail": mail,
               "launches": launches, "elapsed_sec": elapsed,
               "dry_run": args.dry_run},
         error="degraded: one or more steps failed" if degraded else None,
         code=2 if degraded else 0)


def main() -> None:
    p = argparse.ArgumentParser(prog="hub.friday", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    b = sub.add_parser("boot")
    b.add_argument("--profile", required=True)
    b.add_argument("--no-mail", action="store_true")
    b.add_argument("--dry-run", action="store_true")

    args = p.parse_args()
    try:
        if args.action == "boot":
            boot(args)
    except KillSwitchEngaged as e:
        emit(False, error=str(e), code=1)
    except Exception as e:
        emit(False, error=f"unhandled {type(e).__name__}: {e}", code=2)


if __name__ == "__main__":
    main()
