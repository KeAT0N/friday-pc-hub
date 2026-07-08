"""
hub/desktop.py — run desktop/GUI commands in the interactive session.

An SSH login (e.g. from the phone) lands in a NON-interactive Windows session,
separate from the logged-in desktop (session 1). GUI actions there — opening
apps, moving windows, media keys, screenshots, even enumerating the user's
windows — fail or happen on an invisible desktop. This module bridges them: a
Scheduled Task running in the interactive session executes the queued command
on the real desktop, and the SSH side relays its output.

Flow (only when NOT already interactive):
  SSH:  write .desktop_request.json  ->  schtasks /run RemoteHubDesktop
  task (session 1):  `desktop worker` reads it, runs the command, writes
        .desktop_result.json
  SSH:  reads the result, prints the module's JSON envelope verbatim.

GUI modules call `ensure_desktop(module, argv)` at the top of main(): a no-op
when already interactive (runs normally), a transparent bridge otherwise.

CLI:
    run <module> <args...>       bridge one command (explicit)
    worker                       task action: execute the queued request
    install   [--confirm]        register the Scheduled Task (dry-run default)
    status                       is the task registered? am I interactive?
    uninstall [--confirm]
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from xml.sax.saxutils import escape

try:  # package import or direct script run
    from hub.safety import KillSwitchEngaged, assert_alive, wait_until
except ImportError:
    from safety import KillSwitchEngaged, assert_alive, wait_until

REPO = Path(__file__).resolve().parent.parent
TASK_NAME = "RemoteHubDesktop"
REQUEST_PATH = Path(__file__).resolve().parent / ".desktop_request.json"
RESULT_PATH = Path(__file__).resolve().parent / ".desktop_result.json"
BRIDGE_TIMEOUT = 25.0
SCHTASKS_TIMEOUT = 20.0

# Modules whose actions target the visible desktop; they self-bridge over SSH.
DESKTOP_MODULES = {"apps", "window", "media", "screen", "clipboard"}


def emit(ok: bool, action: str, data=None, error: str | None = None) -> None:
    print(json.dumps(
        {"ok": ok, "action": action, "data": data, "error": error},
        ensure_ascii=True))
    sys.exit(0 if ok else 1)


# ---------------------------------------------------------------- session probe

def in_interactive_session() -> bool:
    """True if this process runs in the active console (logged-in) session —
    i.e. GUI actions reach the real desktop. False under SSH / a service."""
    try:
        k = ctypes.windll.kernel32
        k.WTSGetActiveConsoleSessionId.restype = wintypes.DWORD
        k.ProcessIdToSessionId.argtypes = [wintypes.DWORD,
                                           ctypes.POINTER(wintypes.DWORD)]
        k.ProcessIdToSessionId.restype = wintypes.BOOL
        console = k.WTSGetActiveConsoleSessionId()
        sid = wintypes.DWORD()
        if not k.ProcessIdToSessionId(os.getpid(), ctypes.byref(sid)):
            return True
        return console != 0xFFFFFFFF and sid.value == console
    except Exception:
        return True  # can't tell -> behave normally (don't block local use)


# ---------------------------------------------------------------- bridge

def _schtasks(argv: list[str]):
    return subprocess.run(["schtasks", *argv], capture_output=True, text=True,
                          timeout=SCHTASKS_TIMEOUT)


def _run_local(module: str, argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", f"hub.{module}", *argv],
                          capture_output=True, text=True, cwd=str(REPO),
                          timeout=BRIDGE_TIMEOUT)


def bridge(module: str, argv: list[str]) -> None:
    """SSH side: queue the command, trigger the task, relay the result. Exits."""
    req = {"id": f"{os.getpid()}-{int(time.time() * 1000)}",
           "module": module, "argv": list(argv)}
    try:
        RESULT_PATH.unlink(missing_ok=True)
        REQUEST_PATH.write_text(json.dumps(req), encoding="utf-8")
    except OSError as e:
        emit(False, "desktop", error=f"could not queue desktop request: {e}")

    r = _schtasks(["/run", "/tn", TASK_NAME])
    if r.returncode != 0:
        emit(False, "desktop", error="desktop bridge is not installed — run "
             "`hub desktop install --confirm` at the PC once "
             f"({r.stderr.strip()[:150]})")

    def ready():
        try:
            res = json.loads(RESULT_PATH.read_text(encoding="utf-8"))
            return res if res.get("id") == req["id"] else None
        except (OSError, ValueError):
            return None

    res = wait_until(ready, BRIDGE_TIMEOUT, poll=0.25)
    if not res:
        emit(False, "desktop",
             error=f"desktop command timed out after {int(BRIDGE_TIMEOUT)}s "
                   "(is someone logged in at the PC?)")
    sys.stdout.write(res.get("stdout", ""))   # relay the module's envelope
    sys.exit(int(res.get("code", 0)))


def ensure_desktop(module: str, argv: list[str]) -> None:
    """Top-of-main() guard for GUI modules: no-op when interactive, else bridge
    the whole invocation into the interactive session and exit."""
    if in_interactive_session():
        return
    bridge(module, argv)


def worker() -> None:
    """Runs IN the interactive session (via the task). Execute the queued
    request on the real desktop and write the result. No stdout envelope."""
    try:
        req = json.loads(REQUEST_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    try:
        r = _run_local(req["module"], req["argv"])
        out = {"id": req["id"], "stdout": r.stdout, "code": r.returncode}
    except Exception as e:
        out = {"id": req["id"], "code": 1,
               "stdout": json.dumps({"ok": False, "action": req.get("module"),
                                     "data": None,
                                     "error": f"desktop worker error: {e}"})}
    try:
        RESULT_PATH.write_text(json.dumps(out), encoding="utf-8")
    except OSError:
        pass


# ---------------------------------------------------------------- task xml

def _current_user() -> str:
    dom = os.environ.get("USERDOMAIN") or os.environ.get("COMPUTERNAME")
    user = os.environ.get("USERNAME") or "user"
    return f"{dom}\\{user}" if dom else user


def build_task_xml(python_exe: str, repo_dir: str, user: str) -> str:
    """Pure: on-demand task, runs `desktop worker` in the interactive session
    (InteractiveToken) at normal privilege, hidden, requests serialized."""
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        "  <RegistrationInfo>\n"
        "    <Description>Remote Hub desktop bridge: runs GUI hub commands in the "
        "interactive session so they reach the real desktop.</Description>\n"
        f"    <URI>\\{TASK_NAME}</URI>\n"
        "  </RegistrationInfo>\n"
        "  <Triggers />\n"
        "  <Principals>\n"
        '    <Principal id="Author">\n'
        f"      <UserId>{escape(user)}</UserId>\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>LeastPrivilege</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>Queue</MultipleInstancesPolicy>\n"
        "    <AllowStartOnDemand>true</AllowStartOnDemand>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <Enabled>true</Enabled>\n"
        "    <Hidden>true</Hidden>\n"
        "    <ExecutionTimeLimit>PT2M</ExecutionTimeLimit>\n"
        "  </Settings>\n"
        '  <Actions Context="Author">\n'
        "    <Exec>\n"
        f"      <Command>{escape(python_exe)}</Command>\n"
        "      <Arguments>-m hub.desktop worker</Arguments>\n"
        f"      <WorkingDirectory>{escape(repo_dir)}</WorkingDirectory>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "</Task>\n"
    )


# ---------------------------------------------------------------- cli verbs

def do_install(args) -> None:
    xml = build_task_xml(sys.executable, str(REPO), _current_user())
    if not args.confirm:
        emit(True, "desktop.install", data={
            "dry_run": True, "task_name": TASK_NAME, "task_xml": xml,
            "note": "re-run with --confirm to register (may need an admin "
                    "terminal if it reports access denied)"})
    fd, path = tempfile.mkstemp(suffix=".xml", prefix="hubdesk_")
    try:
        os.write(fd, xml.encode("utf-16"))
        os.close(fd)
        r = _schtasks(["/create", "/tn", TASK_NAME, "/xml", path, "/f"])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    ok = r.returncode == 0
    err = None if ok else (r.stderr.strip()[:250] or f"exit {r.returncode}")
    if not ok and "denied" in (err or "").lower():
        err += " — re-run this in an elevated (admin) terminal."
    emit(ok, "desktop.install",
         data={"task_name": TASK_NAME, "registered": ok}, error=err)


def do_status(args) -> None:
    r = _schtasks(["/query", "/tn", TASK_NAME])
    emit(True, "desktop.status", data={
        "task_name": TASK_NAME, "registered": r.returncode == 0,
        "this_session_interactive": in_interactive_session()})


def do_uninstall(args) -> None:
    if not args.confirm:
        emit(True, "desktop.uninstall", data={"dry_run": True,
             "task_name": TASK_NAME})
    r = _schtasks(["/delete", "/tn", TASK_NAME, "/f"])
    ok = r.returncode == 0
    emit(ok, "desktop.uninstall", data={"task_name": TASK_NAME, "removed": ok},
         error=None if ok else (r.stderr.strip()[:200] or f"exit {r.returncode}"))


def do_run(args) -> None:
    """Explicit bridge: `desktop run apps open spotify`."""
    if not args.command:
        emit(False, "desktop.run", error="usage: desktop run <module> <args...>")
    module, argv = args.command[0], args.command[1:]
    if in_interactive_session():           # already local: just run it
        r = _run_local(module, argv)
        sys.stdout.write(r.stdout)
        sys.exit(r.returncode)
    bridge(module, argv)


def main() -> None:
    p = argparse.ArgumentParser(prog="hub.desktop", description=__doc__)
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("worker")
    sub.add_parser("status")
    ins = sub.add_parser("install"); ins.add_argument("--confirm", action="store_true")
    un = sub.add_parser("uninstall"); un.add_argument("--confirm", action="store_true")
    rn = sub.add_parser("run"); rn.add_argument("command", nargs=argparse.REMAINDER)

    args = p.parse_args()
    try:
        if args.action == "worker":
            worker()
            return
        assert_alive(f"desktop.{args.action}")
        {"status": do_status, "install": do_install, "uninstall": do_uninstall,
         "run": do_run}[args.action](args)
    except KillSwitchEngaged as e:
        emit(False, args.action, error=str(e))
    except Exception as e:
        emit(False, args.action, error=f"unhandled {type(e).__name__}: {e}")


if __name__ == "__main__":
    main()
