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

# RGB / AlienFX. AWCC has no stable public color CLI, so this stays a
# fail-soft scaffold: resolve a controller, attempt a bounded call, else inert.
# Override the binary and arg template via env to wire the real control path.
RGB_TIMEOUT = 10.0
ALIENFX_CLI_ENV = "HUB_ALIENFX_CLI"
ALIENFX_ARGS_ENV = "HUB_ALIENFX_ARGS"
ALIENFX_CANDIDATES = (
    r"C:\Program Files\Dell\AlienFX\AlienFX.exe",
    r"C:\Program Files\Alienware\Command Center\AWCC.exe",
    r"C:\Program Files\Dell\CommandCenter\AWCC.exe",
)
RGB_THEMES = {
    "green": "00FF00", "red": "FF0000", "purple": "800080", "orange": "FF7A00",
    "blue": "0000FF", "cyan": "00FFFF", "white": "FFFFFF", "off": "000000",
}

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


def run_kills(kill_list: list[str] | None, dry_run: bool) -> list[dict] | None:
    """Terminate named background/tray processes via the guarded apps.kill CLI.
    Dry-run only counts matches (read-only apps.query); never terminates."""
    if not kill_list:
        return None
    results = []
    for name in kill_list:
        if name.lower() in SELF_PROTECT:  # friday guard: never kill what wipe protects
            results.append({"name": name, "matched": 0, "ok": True,
                            "skipped": "protected"})
            continue
        # Preview uses apps.kill --dry-run so the count reflects the SAME
        # exact-name + current-user selection the live kill will act on.
        argv = ["kill", "--name", name] + (["--dry-run"] if dry_run else [])
        r = run_module("apps", argv)
        d = r.get("data") or {}
        results.append({"name": name, "matched": d.get("matched", 0),
                        "terminated": d.get("terminated"),
                        "ok": None if dry_run else bool(r.get("ok")),
                        "error": None if r.get("ok") else r.get("error")})
    return results


def narrate_kills(kills: list[dict], dry_run: bool) -> None:
    for k in kills:
        if k.get("skipped"):
            state = f"SKIPPED ({k['skipped']})"
        elif dry_run:
            state = f"would kill {k['matched']}"
        elif k["ok"]:
            state = f"killed {k['matched']}" if k["matched"] else "none running"
        else:
            state = f"FAIL ({k.get('error')})"
        log(f"kill: {k['name']} -> {state}")


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


# ---------------------------------------------------------------- resource report

def resource_report(spec: dict | None, dry_run: bool) -> dict | None:
    """Read-only snapshot of the heaviest processes (RAM + CPU) so an
    optimize pass can show the win. Always safe — never mutates anything."""
    if not spec:
        return None
    n = str(spec.get("top", 8))
    mem = run_module("system", ["top", "--by", "memory", "--count", n])
    cpu = run_module("system", ["top", "--by", "cpu", "--count", n])
    return {
        "top_memory": mem["data"] if mem.get("ok") else {"error": mem.get("error")},
        "top_cpu": cpu["data"] if cpu.get("ok") else {"error": cpu.get("error")},
    }


def narrate_report(report: dict) -> None:
    mem = report.get("top_memory")
    if isinstance(mem, list) and mem:
        log("report: top RAM -> " + ", ".join(
            f"{r['name']} {r['memory_mb']}MB" for r in mem[:3]))


# ---------------------------------------------------------------- rgb / alienfx

def _resolve_alienfx() -> str | None:
    override = os.environ.get(ALIENFX_CLI_ENV)
    if override:
        return override if os.path.isfile(override) else None
    return next((c for c in ALIENFX_CANDIDATES if os.path.isfile(c)), None)


def apply_rgb(rgb, dry_run: bool) -> dict | None:
    """Set hardware RGB zones to a theme/color via the AlienFX controller.
    Fail-soft scaffold: inert (reported, never fatal) if no controller is
    found or the call errors; bounded by RGB_TIMEOUT so it can't hang FRIDAY."""
    if not rgb:
        return None
    color = RGB_THEMES.get(str(rgb).lower(), str(rgb))
    base = {"theme": rgb, "color": color}
    if dry_run:
        return {"rgb": {**base, "ok": None, "dry_run": True,
                        "note": "would set AlienFX zones"}}

    cli = _resolve_alienfx()
    if not cli:
        return {"rgb": {**base, "ok": False,
                        "error": "AlienFX/AWCC controller not found - RGB layer "
                                 f"inert (set {ALIENFX_CLI_ENV} to enable)"}}
    tmpl = os.environ.get(ALIENFX_ARGS_ENV, "--zone all --color {color}")
    argv = [cli, *tmpl.format(color=color).split()]
    try:
        p = subprocess.run(argv, capture_output=True, text=True,
                           timeout=RGB_TIMEOUT, cwd=str(REPO))
        ok = p.returncode == 0
        return {"rgb": {**base, "cli": os.path.basename(cli),
                        "returncode": p.returncode, "ok": ok,
                        "error": None if ok else
                        (p.stderr.strip()[:160] or f"exit {p.returncode}")}}
    except subprocess.TimeoutExpired:
        return {"rgb": {**base, "ok": False,
                        "error": f"AlienFX call timed out after {RGB_TIMEOUT}s"}}
    except OSError as e:
        return {"rgb": {**base, "ok": False, "error": f"{type(e).__name__}: {e}"}}


# ---------------------------------------------------------------- power step

# A scene may power down the session, but ONLY via reversible verbs. shutdown/
# restart are deliberately excluded: friday auto-passes --confirm (it is the
# trusted orchestrator), so allowing them here would let a mere profile edit
# silently confirm an irreversible shutdown. Those stay a direct power.py call.
SCENE_POWER_VERBS = {"lock", "monitor-off", "sleep", "hibernate"}


def run_power(verb: str | None, dry_run: bool) -> dict | None:
    """Run a reversible power.py verb as the final scene step. dry-run previews
    (no --confirm); a live run passes --confirm since friday is trusted."""
    if not verb:
        return None
    if verb not in SCENE_POWER_VERBS:
        return {"verb": verb, "ok": False, "error":
                f"{verb!r} not allowed in a scene; reversible only "
                f"({sorted(SCENE_POWER_VERBS)})"}
    r = run_module("power", [verb] + ([] if dry_run else ["--confirm"]))
    if dry_run:
        return {"verb": verb, "ok": None, "dry_run": True,
                "plan": (r.get("data") or {}).get("plan")}
    return {"verb": verb, "ok": bool(r.get("ok")),
            "error": None if r.get("ok") else r.get("error")}


def narrate_rgb(rgb_res: dict | None, dry_run: bool) -> None:
    if rgb_res is None:
        return
    r = rgb_res["rgb"]
    state = "PLAN" if dry_run else ("OK" if r["ok"] else f"inert ({r.get('error')})")
    log(f"rgb: {r['theme']} (#{r['color']}) -> {state}")


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
    rgb = apply_rgb(profile.get("rgb"), args.dry_run)
    narrate_rgb(rgb, args.dry_run)

    elapsed = round(time.monotonic() - t0, 1)
    degraded = (bool(mail) and any("error" in v for v in mail.values())) \
        or any(l.get("ok") is False for l in launches) \
        or (rgb is not None and rgb["rgb"].get("ok") is False)
    log(f"booted '{args.profile}' in {elapsed}s" + (" (DEGRADED)" if degraded else ""))
    emit(not degraded, code=2 if degraded else 0,
         error="degraded: one or more steps failed" if degraded else None,
         data={"profile": args.profile, "preflight": vault or None, "mail": mail,
               "launches": launches, "rgb": rgb, "elapsed_sec": elapsed,
               "dry_run": args.dry_run})


def trigger(args) -> None:
    t0 = time.monotonic()
    profile = load_profile(args.profile)
    gate(args.profile)
    vault = require_credentials(args.profile, profile.get("requires", []))

    report = resource_report(profile.get("report"), args.dry_run)
    if report is not None:
        narrate_report(report)

    wipe = None
    if profile.get("wipe"):
        wipe = wipe_windows(profile["wipe"].get("protect", []), args.dry_run)
        if wipe.get("ok"):
            narrate_wipe(wipe, args.dry_run)
        else:
            log(f"wipe: FAILED ({wipe.get('error')})")

    kills = run_kills(profile.get("kill"), args.dry_run)
    if kills is not None:
        narrate_kills(kills, args.dry_run)

    launches = run_launches(profile.get("launch", []), args.dry_run)

    sh = smart_home(profile.get("smart_home"), args.dry_run)
    if sh is not None:
        w = sh["wemo"]
        state = "PLAN" if args.dry_run else ("OK" if w["ok"] else f"SKIP ({w.get('error')})")
        log(f"smart-home: wemo '{w['device']}' {w['action']} -> {state}")

    rgb = apply_rgb(profile.get("rgb"), args.dry_run)
    narrate_rgb(rgb, args.dry_run)

    # power-down runs LAST (lighting/wipe first, then lock the session)
    power = run_power(profile.get("power"), args.dry_run)
    if power is not None:
        state = "PLAN" if args.dry_run else (
            "OK" if power["ok"] else f"FAIL ({power.get('error')})")
        log(f"power: {power['verb']} -> {state}")

    elapsed = round(time.monotonic() - t0, 1)
    degraded = any(l.get("ok") is False for l in launches) \
        or (wipe is not None and not wipe.get("ok")) \
        or (kills is not None and any(k.get("ok") is False for k in kills)) \
        or (sh is not None and sh["wemo"].get("ok") is False) \
        or (rgb is not None and rgb["rgb"].get("ok") is False) \
        or (power is not None and power.get("ok") is False)
    log(f"triggered '{args.profile}' in {elapsed}s" + (" (DEGRADED)" if degraded else ""))
    emit(not degraded, code=2 if degraded else 0,
         error="degraded: one or more steps failed" if degraded else None,
         data={"profile": args.profile, "preflight": vault or None,
               "report": report, "wipe": wipe, "kills": kills,
               "launches": launches, "smart_home": sh, "rgb": rgb,
               "power": power, "elapsed_sec": elapsed, "dry_run": args.dry_run})


# ---------------------------------------------------------------- status scene

STATUS_VAULT_SERVICES = ("icloud_mail", "gmail", "github")


def vault_audit(services) -> dict:
    """Non-aborting existence audit of vault services (unlike require_credentials
    which fail-closes a boot). Pure read: only booleans, never values."""
    out = {}
    for svc in services:
        r = run_module("credentials", ["check", "--service", svc,
                                       "--account", VAULT_ACCOUNT,
                                       "--provider", "keyring"])
        out[svc] = bool(r.get("ok") and (r.get("data") or {}).get("exists"))
    return out


def status(args) -> None:
    """Read-only health dashboard: composes the read-only module CLIs into one
    snapshot. Never acts, so it also REPORTS the kill-switch rather than obeying
    it — the one scene you want to work even when the hub is disarmed."""
    t0 = time.monotonic()
    ks = run_module("safety", ["kill", "status"])
    kill_switch = ks.get("data") if ks.get("ok") else {"error": ks.get("error")}
    log("kill-switch: " + ("ENGAGED" if (kill_switch or {}).get("engaged")
                           else "clear"))

    system = run_module("system", ["snapshot", "--top", "5", "--sample", "0.3"])
    net = run_module("net", ["status"])
    mail = None if args.no_mail else mail_glance()
    vault = vault_audit(STATUS_VAULT_SERVICES)

    sysd = system["data"] if system.get("ok") else {"error": system.get("error")}
    netd = net["data"] if net.get("ok") else {"error": net.get("error")}
    if system.get("ok"):
        disks = ", ".join(f"{d['mount']} {d['free_gb']}GB free"
                          for d in sysd["disk"][:2])
        log(f"system: cpu {sysd['cpu']['percent']}%  "
            f"mem {sysd['memory']['percent']}%  disk {disks}")
    else:
        log(f"system: ERR ({system.get('error')})")
    if net.get("ok"):
        log(f"net: {'online' if netd['online'] else 'OFFLINE'}  ip {netd['local_ip']}")
    else:
        log(f"net: ERR ({net.get('error')})")
    if mail is not None:
        log("mail: " + " - ".join(
            f"{p} {i['unread']} unread" if "unread" in i else f"{p} ERR"
            for p, i in mail.items()))
    log("vault: " + "  ".join(
        f"{k} {'OK' if v else 'MISSING'}" for k, v in vault.items()))

    elapsed = round(time.monotonic() - t0, 1)
    degraded = not system.get("ok") or not net.get("ok") \
        or (mail is not None and any("error" in v for v in mail.values()))
    log(f"status in {elapsed}s" + (" (DEGRADED)" if degraded else ""))
    emit(not degraded, code=2 if degraded else 0,
         error="degraded: a read failed" if degraded else None,
         data={"kill_switch": kill_switch, "system": sysd, "net": netd,
               "mail": mail, "vault": vault, "elapsed_sec": elapsed})


# ---------------------------------------------------------------- respond (5b tripwire)
# The stateless handler the OS launches on a security event. Read-only + fail-
# soft + bounded: it never mutates anything, it only reads intruder signals and
# fires a desktop toast when a DATA-defined threshold is crossed. Kill-switch
# gates it (disarmed hub => no autonomous action); a cooldown marker debounces
# an event storm; the threshold lives in profiles.json security_watch (data).

SECURITY_WATCH_DEFAULTS = {"failed_logon_count": 10, "window_sec": 3600,
                           "cooldown_sec": 300, "defender_any": True}
DEFAULT_COOLDOWN_FILE = Path(__file__).resolve().parent / ".respond_cooldown"


def load_security_watch() -> dict:
    try:
        cfg = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(SECURITY_WATCH_DEFAULTS)
    return {**SECURITY_WATCH_DEFAULTS, **(cfg.get("security_watch") or {})}


def _cooldown_path() -> Path:
    return Path(os.environ.get("HUB_RESPOND_COOLDOWN_FILE", DEFAULT_COOLDOWN_FILE))


def _cooldown_remaining(cooldown_sec: float) -> float:
    f = _cooldown_path()
    if not f.exists():
        return 0.0
    try:
        ts = float(f.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0.0  # unreadable marker => treat as expired (fail toward acting)
    return max(0.0, cooldown_sec - (time.time() - ts))


def _arm_cooldown() -> None:
    try:
        _cooldown_path().write_text(str(time.time()), encoding="utf-8")
    except OSError:
        pass


def respond(args) -> None:
    t0 = time.monotonic()
    gate("respond")                     # kill-switch: disarmed hub acts on nothing
    sw = load_security_watch()

    if not args.dry_run:
        rem = _cooldown_remaining(sw["cooldown_sec"])
        if rem > 0:                     # anti-storm: recently alerted, stay quiet
            log(f"cooling down ({int(rem)}s left) - skipping scan")
            emit(True, code=0, data={"triggered": False, "cooling_down": True,
                                     "cooldown_remaining_sec": int(rem),
                                     "dry_run": False})

    hours = max(1, (sw["window_sec"] + 3599) // 3600)   # window_sec -> ceil hours
    intr = run_module("security", ["intruders", "--hours", str(hours), "--max", "50"])
    idata = intr.get("data") or {}
    fl = idata.get("failed_logons") or {}
    dd = idata.get("defender_detections") or {}

    reasons = []
    fl_count = fl.get("count") if fl.get("available") else None
    if isinstance(fl_count, int) and fl_count >= sw["failed_logon_count"]:
        reasons.append(f"{fl_count} failed logins in ~{hours}h "
                       f"(threshold {sw['failed_logon_count']})")
    dd_count = dd.get("count") if dd.get("available") else None
    if sw["defender_any"] and isinstance(dd_count, int) and dd_count > 0:
        reasons.append(f"{dd_count} Defender threat detection(s)")
    triggered = bool(reasons)
    log("signals: " + (f"TRIGGERED - {'; '.join(reasons)}" if triggered else "clear"))

    notified = False
    if triggered and not args.dry_run:
        nr = run_module("notify", ["send", "--title", "Security alert (FRIDAY)",
                                   "--message", "; ".join(reasons)[:480],
                                   "--duration", "long"])
        notified = bool(nr.get("ok"))
        _arm_cooldown()
        log(f"ALERT -> notify {'ok' if notified else 'FAILED'}; "
            f"cooldown armed {sw['cooldown_sec']}s")
    elif triggered and args.dry_run:
        log("DRY-RUN: would alert + arm cooldown (no toast, no marker written)")

    degraded = not intr.get("ok")
    emit(not degraded, code=2 if degraded else 0,
         error="degraded: could not read intruder signals" if degraded else None,
         data={"triggered": triggered, "reasons": reasons, "notified": notified,
               "cooling_down": False, "dry_run": args.dry_run,
               "signals": {"failed_logons": fl, "defender_detections": dd},
               "thresholds": sw, "window_hours": hours,
               "elapsed_sec": round(time.monotonic() - t0, 1)})


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

    st = sub.add_parser("status")
    st.add_argument("--no-mail", action="store_true")

    rs = sub.add_parser("respond")
    rs.add_argument("--dry-run", action="store_true")

    args = p.parse_args()
    try:
        {"boot": boot, "trigger": trigger, "status": status,
         "respond": respond}[args.action](args)
    except KillSwitchEngaged as e:
        emit(False, error=str(e), code=1)
    except Exception as e:
        emit(False, error=f"unhandled {type(e).__name__}: {e}", code=2)


if __name__ == "__main__":
    main()
