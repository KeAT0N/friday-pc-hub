"""
hub/friday.py — FRIDAY orchestrator (command router + scene trigger).

Composes the tested hub module CLIs into deterministic routines. Not a sensor,
not LLM logic: it reads data-defined profiles, verifies preconditions
fail-closed, and drives launches / a safe window-wipe / a smart-home layer.

    python -m hub.friday boot    --profile dev   [--no-mail] [--dry-run]
    python -m hub.friday trigger sit-down        [--dry-run]
    python -m hub.friday trigger chill           [--dry-run]

Profiles live in hub/profiles.json (data, not code):
  requires:   [service...]     offline vault preconditions (fail-closed)
  launch:     [{type: code|app|url, ...}]
  wipe:       {protect: [pattern...]}   graceful-close other windows
  smart_home: {wemo: {device, action}} local UPnP scene control

Safe wipe: closes only visible top-level windows via graceful WM_CLOSE (never
--force, so unsaved-work prompts are honoured and nothing is killed). TWO
protection layers keep the dev environment alive — the profile's `protect`
patterns (matched against process name AND title) plus an always-on
SELF_PROTECT list covering Claude, VS Code, the shell/terminal, python, and
the Windows shell. A window is closed only if it matches neither layer.

Output: stdout = JSON envelope; stderr = live [FRIDAY] narration.
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

REPO = Path(__file__).resolve().parent.parent
PROFILES_PATH = Path(__file__).resolve().parent / "profiles.json"
VAULT_ACCOUNT = "keatondavey"
MAIL_PROVIDERS = ("gmail", "icloud")
MODULE_TIMEOUT = 45.0
CLOSE_TIMEOUT = 6
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Always protected from a wipe, regardless of profile — the developer
# environment, the orchestrator's own process, the shell it runs in, and the
# Windows shell. Belt-and-suspenders on top of profile `protect` patterns.
SELF_PROTECT = {
    "claude.exe", "code.exe",                       # dev env (never close)
    "explorer.exe", "windowsterminal.exe", "openconsole.exe", "conhost.exe",
    "cmd.exe", "powershell.exe", "pwsh.exe",        # shells running/hosting us
    "python.exe", "py.exe",                          # friday itself + submodules
    "systemsettings.exe", "shellexperiencehost.exe", "startmenuexperiencehost.exe",
    "searchhost.exe", "searchapp.exe", "textinputhost.exe",
    "applicationframehost.exe", "lockapp.exe", "sihost.exe", "dwm.exe",
    "taskmgr.exe",
}


def log(msg: str) -> None:
    print(f"[FRIDAY] {msg}", file=sys.stderr, flush=True)


def emit(ok: bool, data=None, error: str | None = None, code: int = 0) -> None:
    print(json.dumps(
        {"ok": ok, "action": "friday", "data": data, "error": error},
        ensure_ascii=True,
    ))
    sys.exit(code)


def run_module(module: str, argv: list[str], timeout: float = MODULE_TIMEOUT) -> dict:
    """Run a hub module CLI, return its parsed JSON envelope (or a synthetic
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


# ---------------------------------------------------------------- shared phases

def load_profile(name: str) -> dict:
    try:
        profiles = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        emit(False, error=f"cannot read profiles.json: {e}", code=1)
    profile = profiles.get(name)
    if not profile:
        emit(False, error=f"unknown profile {name!r}; known: {sorted(profiles)}",
             code=1)
    return profile


def gate(name: str) -> None:
    try:
        assert_alive("friday")
    except KillSwitchEngaged as e:
        log("gate BLOCKED - kill-switch engaged")
        emit(False, data={"profile": name}, error=str(e), code=1)
    log("gate ok - kill-switch clear")


def require_credentials(name: str, required: list[str]) -> dict:
    if not required:
        return {}
    vault = {}
    for svc in required:
        r = run_module("credentials", ["check", "--service", svc,
                                       "--account", VAULT_ACCOUNT,
                                       "--provider", "keyring"])
        vault[svc] = bool(r.get("ok") and (r.get("data") or {}).get("exists"))
    log("vault: " + "  ".join(
        f"{k} {'OK' if v else 'MISSING'}" for k, v in vault.items()))
    missing = [k for k, v in vault.items() if not v]
    if missing:
        log(f"ABORT - required credentials missing: {missing}")
        emit(False, data={"profile": name, "preflight": vault},
             error=f"missing required credentials: {missing}", code=1)
    return vault


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
    label = entry.get("label") or entry.get("path") or entry.get("target")
    try:
        if t == "code":
            path = entry.get("path", "")
            if not os.path.isdir(path):
                return {"type": t, "label": label, "ok": False,
                        "error": "folder not found"}
            subprocess.Popen(["cmd", "/c", "code", path], cwd=str(REPO),
                             creationflags=CREATE_NO_WINDOW)
            return {"type": t, "label": label, "ok": True}
        if t in ("app", "url"):
            argv = ["launch", "--target", entry.get("target", "")]
            if entry.get("args"):
                argv += ["--args", *entry["args"]]
            r = run_module("apps", argv)
            return {"type": t, "label": label, "ok": bool(r.get("ok")),
                    "error": None if r.get("ok") else r.get("error")}
        return {"type": t, "label": label, "ok": False,
                "error": f"unknown launch type {t!r}"}
    except Exception as e:
        return {"type": t, "label": label, "ok": False,
                "error": f"{type(e).__name__}: {e}"}


def run_launches(entries: list[dict], dry_run: bool) -> list[dict]:
    launches = []
    if dry_run:
        for e in entries:
            label = e.get("label") or e.get("path") or e.get("target")
            log(f"launch: {e.get('type')} -> {label} PLAN")
            launches.append({"type": e.get("type"), "label": label,
                             "ok": None, "dry_run": True})
        return launches
    for e in entries:
        res = launch_entry(e)
        log(f"launch: {res['type']} -> {res.get('label')} "
            + ("OK" if res["ok"] else f"FAIL ({res.get('error')})"))
        launches.append(res)
    return launches


# ---------------------------------------------------------------- wipe

def wipe_windows(protect_patterns: list[str], dry_run: bool) -> dict:
    """Graceful-close every visible top-level window EXCEPT those protected by
    a profile pattern (process name or title) or the SELF_PROTECT list."""
    r = run_module("apps", ["list"])
    if not r.get("ok"):
        return {"ok": False, "error": f"could not enumerate windows: {r.get('error')}",
                "protected": [], "closed": []}
    patterns = [p.lower() for p in protect_patterns]
    protected, closed = [], []
    for w in r.get("data", []):
        name = (w.get("process") or "").lower()
        title = (w.get("title") or "").lower()
        item = {"hwnd": w.get("hwnd"), "process": w.get("process"),
                "title": w.get("title")}

        if name in SELF_PROTECT:
            protected.append({**item, "reason": "self/system"})
            continue
        hit = next((p for p in patterns if p in name or p in title), None)
        if hit:
            protected.append({**item, "reason": f"pattern:{hit}"})
            continue

        if dry_run:
            closed.append({**item, "ok": None, "dry_run": True})
            continue
        # Graceful only (no --force): a window with unsaved changes survives
        # WM_CLOSE and is reported as kept, never killed.
        cr = run_module("apps", ["close", "--hwnd", str(w["hwnd"]),
                                 "--timeout", str(CLOSE_TIMEOUT)])
        closed.append({**item, "ok": bool(cr.get("ok")),
                       "error": None if cr.get("ok") else cr.get("error")})
    return {"ok": True, "protect_patterns": protect_patterns, "dry_run": dry_run,
            "protected": protected, "closed": closed}


def narrate_wipe(wipe: dict, dry_run: bool) -> None:
    verb = "would close" if dry_run else "closed"
    log(f"wipe: protected {len(wipe['protected'])} "
        f"(dev env + system safe) - {verb} {len(wipe['closed'])}")
    for p in wipe["protected"]:
        log(f"  PROTECT {p['process']} '{(p['title'] or '')[:36]}' [{p['reason']}]")
    for c in wipe["closed"]:
        if dry_run:
            st = "PLAN"
        elif c["ok"]:
            st = "closed"
        else:
            st = f"KEPT ({(c.get('error') or '')[:36]})"
        log(f"  {c['process']} '{(c['title'] or '')[:36]}' -> {st}")


# ---------------------------------------------------------------- smart home

def smart_home(spec: dict | None, dry_run: bool) -> dict | None:
    """Local UPnP smart-home layer. Toggles a Wemo Wi-Fi switch via pywemo,
    which handles SSDP/UPnP discovery on the LAN. Lazy-imported and fail-soft:
    if pywemo is absent the layer is inert (pip install pywemo to enable)."""
    wemo = (spec or {}).get("wemo")
    if not wemo:
        return None
    device, action = wemo.get("device", ""), wemo.get("action", "toggle")
    base = {"device": device, "action": action}

    if dry_run:
        return {"wemo": {**base, "ok": None, "dry_run": True,
                         "note": "would UPnP-discover then toggle"}}
    try:
        import pywemo
    except ImportError:
        return {"wemo": {**base, "ok": False,
                         "error": "pywemo not installed — layer inert "
                                  "(pip install pywemo)"}}
    try:
        devices = pywemo.discover_devices()  # SSDP broadcast; internally bounded
        match = next((d for d in devices if device.lower() in d.name.lower()), None)
        if not match:
            return {"wemo": {**base, "ok": False,
                             "error": f"no Wemo matching {device!r} on LAN",
                             "discovered": [d.name for d in devices]}}
        {"toggle": match.toggle, "on": match.on,
         "off": match.off}.get(action, match.toggle)()
        return {"wemo": {**base, "ok": True, "matched": match.name,
                         "state": match.get_state()}}
    except Exception as e:
        return {"wemo": {**base, "ok": False, "error": f"{type(e).__name__}: {e}"}}


# ---------------------------------------------------------------- entrypoints

def boot(args) -> None:
    t0 = time.monotonic()
    profile = load_profile(args.profile)
    gate(args.profile)
    vault = require_credentials(args.profile, profile.get("requires", []))

    mail = None
    if args.no_mail:
        log("mail: skipped (--no-mail)")
    else:
        mail = mail_glance()
        log("mail: " + " - ".join(
            f"{p} {i['unread']} unread" if "unread" in i
            else f"{p} ERR ({i['error']})" for p, i in mail.items()))

    launches = run_launches(profile.get("launch", []), args.dry_run)

    elapsed = round(time.monotonic() - t0, 1)
    degraded = (bool(mail) and any("error" in v for v in mail.values())) \
        or any(l.get("ok") is False for l in launches)
    log(f"booted '{args.profile}' in {elapsed}s" + (" (DEGRADED)" if degraded else ""))
    emit(not degraded, code=2 if degraded else 0,
         error="degraded: one or more steps failed" if degraded else None,
         data={"profile": args.profile, "preflight": vault or None, "mail": mail,
               "launches": launches, "elapsed_sec": elapsed, "dry_run": args.dry_run})


def trigger(args) -> None:
    t0 = time.monotonic()
    profile = load_profile(args.profile)
    gate(args.profile)
    vault = require_credentials(args.profile, profile.get("requires", []))

    wipe = None
    if profile.get("wipe"):
        wipe = wipe_windows(profile["wipe"].get("protect", []), args.dry_run)
        if wipe.get("ok"):
            narrate_wipe(wipe, args.dry_run)
        else:
            log(f"wipe: FAILED ({wipe.get('error')})")

    launches = run_launches(profile.get("launch", []), args.dry_run)

    sh = smart_home(profile.get("smart_home"), args.dry_run)
    if sh is not None:
        w = sh["wemo"]
        state = "PLAN" if args.dry_run else ("OK" if w["ok"] else f"SKIP ({w.get('error')})")
        log(f"smart-home: wemo '{w['device']}' {w['action']} -> {state}")

    elapsed = round(time.monotonic() - t0, 1)
    degraded = any(l.get("ok") is False for l in launches) \
        or (wipe is not None and not wipe.get("ok")) \
        or (sh is not None and sh["wemo"].get("ok") is False)
    log(f"triggered '{args.profile}' in {elapsed}s" + (" (DEGRADED)" if degraded else ""))
    emit(not degraded, code=2 if degraded else 0,
         error="degraded: one or more steps failed" if degraded else None,
         data={"profile": args.profile, "preflight": vault or None, "wipe": wipe,
               "launches": launches, "smart_home": sh,
               "elapsed_sec": elapsed, "dry_run": args.dry_run})


def main() -> None:
    p = argparse.ArgumentParser(prog="hub.friday", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)

    b = sub.add_parser("boot")
    b.add_argument("--profile", required=True)
    b.add_argument("--no-mail", action="store_true")
    b.add_argument("--dry-run", action="store_true")

    t = sub.add_parser("trigger")
    t.add_argument("profile")
    t.add_argument("--dry-run", action="store_true")

    args = p.parse_args()
    try:
        {"boot": boot, "trigger": trigger}[args.action](args)
    except KillSwitchEngaged as e:
        emit(False, error=str(e), code=1)
    except Exception as e:
        emit(False, error=f"unhandled {type(e).__name__}: {e}", code=2)


if __name__ == "__main__":
    main()
