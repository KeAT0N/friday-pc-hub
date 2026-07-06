"""Unit + CLI tests for hub.friday — the orchestrator.

The load-bearing logic is the safe-wipe protection layers (never close the dev
env / unsaved work) and the kill-skip guard. Those are tested with
friday.run_module mocked so no real window is closed and no process killed.
Live tests only use --dry-run (read-only planning) and exit-code paths.

friday uses a richer exit scheme (0 clean / 1 aborted / 2 degraded), so these
assert codes directly rather than via the strict envelope contract.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hub import friday
from tests._helpers import run_cli


class _Emitted(Exception):
    pass


STATUS_DEFAULTS = {
    "safety": {"ok": True, "data": {"engaged": False, "file": "f", "detail": None}},
    "system": {"ok": True, "data": {"cpu": {"percent": 5.0},
                                    "memory": {"percent": 40.0},
                                    "disk": [{"mount": "C:", "free_gb": 100.0}]}},
    "net": {"ok": True, "data": {"online": True, "local_ip": "1.2.3.4",
                                 "hostname": "h"}},
}


def wins(*rows):
    """Build an apps-list style window dict list."""
    return [{"hwnd": h, "process": p, "title": t} for h, p, t in rows]


class TestWipeProtection(unittest.TestCase):
    """Two protection layers: SELF_PROTECT (always) + profile patterns."""

    def _wipe(self, windows, protect, dry_run, close_results=None):
        calls = []

        def fake(module, argv, timeout=friday.MODULE_TIMEOUT):
            calls.append((module, list(argv)))
            if argv[0] == "list":
                return {"ok": True, "data": windows}
            if argv[0] == "close":
                hwnd = int(argv[argv.index("--hwnd") + 1])
                return (close_results or {}).get(hwnd, {"ok": True})
            return {"ok": False, "error": "unexpected"}

        with mock.patch.object(friday, "run_module", fake):
            result = friday.wipe_windows(protect, dry_run)
        return result, calls

    def test_self_protect_always_kept(self):
        w = wins((1, "claude.exe", "Claude"), (2, "code.exe", "repo"),
                 (3, "explorer.exe", "Desktop"))
        result, calls = self._wipe(w, protect=[], dry_run=True)
        kept = {p["hwnd"] for p in result["protected"]}
        self.assertEqual(kept, {1, 2, 3})
        self.assertEqual(result["closed"], [])
        # dry-run must never issue a close
        self.assertFalse(any(c[1][0] == "close" for c in calls))

    def test_profile_pattern_matches_name_or_title(self):
        w = wins((10, "FortniteClient.exe", "Fortnite"),   # name match
                 (11, "game.exe", "My Fortnite Match"),      # title match
                 (12, "chrome.exe", "Gmail"))                # no match -> close
        result, _ = self._wipe(w, protect=["fortnite"], dry_run=True)
        kept = {p["hwnd"] for p in result["protected"]}
        closed = {c["hwnd"] for c in result["closed"]}
        self.assertEqual(kept, {10, 11})
        self.assertEqual(closed, {12})
        reasons = {p["hwnd"]: p["reason"] for p in result["protected"]}
        self.assertTrue(reasons[10].startswith("pattern:"))

    def test_live_close_only_unprotected(self):
        w = wins((1, "claude.exe", "Claude"),      # self-protect
                 (2, "notepad.exe", "untitled"),    # close
                 (3, "chrome.exe", "web"))          # close
        result, calls = self._wipe(w, protect=[], dry_run=False)
        closed_hwnds = sorted(
            int(c[1][c[1].index("--hwnd") + 1])
            for c in calls if c[1][0] == "close")
        self.assertEqual(closed_hwnds, [2, 3])  # never hwnd 1
        self.assertTrue(all(c["ok"] for c in result["closed"]))

    def test_unsaved_window_reported_kept_not_killed(self):
        w = wins((5, "word.exe", "Doc *unsaved"))
        result, _ = self._wipe(
            w, protect=[], dry_run=False,
            close_results={5: {"ok": False, "error": "survived WM_CLOSE"}})
        # graceful-only: the window survives and is reported ok False, not forced
        self.assertEqual(len(result["closed"]), 1)
        self.assertFalse(result["closed"][0]["ok"])


class TestRunKills(unittest.TestCase):
    def _kills(self, kill_list, dry_run):
        calls = []

        def fake(module, argv, timeout=friday.MODULE_TIMEOUT):
            calls.append(list(argv))
            return {"ok": True, "data": {"matched": 2, "terminated": []}}

        with mock.patch.object(friday, "run_module", fake):
            result = friday.run_kills(kill_list, dry_run)
        return result, calls

    def test_none_when_empty(self):
        result, calls = self._kills(None, dry_run=True)
        self.assertIsNone(result)
        self.assertEqual(calls, [])

    def test_self_protected_name_skipped(self):
        result, calls = self._kills(["python.exe", "Discord.exe"], dry_run=True)
        by_name = {r["name"]: r for r in result}
        self.assertEqual(by_name["python.exe"]["skipped"], "protected")
        self.assertEqual(by_name["python.exe"]["matched"], 0)
        # python.exe must never reach apps.kill; Discord.exe does (with --dry-run)
        self.assertFalse(any("python.exe" in c for c in calls))
        discord_call = next(c for c in calls if "Discord.exe" in c)
        self.assertIn("--dry-run", discord_call)

    def test_live_kill_omits_dry_run_flag(self):
        _, calls = self._kills(["Discord.exe"], dry_run=False)
        self.assertNotIn("--dry-run", calls[0])


class TestApplyRGB(unittest.TestCase):
    def test_none_when_absent(self):
        self.assertIsNone(friday.apply_rgb(None, dry_run=False))

    def test_dry_run_plans_theme_color(self):
        r = friday.apply_rgb("green", dry_run=True)["rgb"]
        self.assertEqual(r["color"], "00FF00")
        self.assertIsNone(r["ok"])
        self.assertTrue(r["dry_run"])

    def test_inert_when_no_controller(self):
        with mock.patch.object(friday, "_resolve_alienfx", return_value=None):
            r = friday.apply_rgb("red", dry_run=False)["rgb"]
        self.assertFalse(r["ok"])
        self.assertIn("inert", r["error"])


class TestSmartHome(unittest.TestCase):
    def test_none_without_wemo(self):
        self.assertIsNone(friday.smart_home(None, dry_run=True))
        self.assertIsNone(friday.smart_home({"other": 1}, dry_run=True))

    def test_dry_run_plans(self):
        sh = friday.smart_home(
            {"wemo": {"device": "Lamp", "action": "on"}}, dry_run=True)
        self.assertIsNone(sh["wemo"]["ok"])
        self.assertTrue(sh["wemo"]["dry_run"])


class TestRunModule(unittest.TestCase):
    def test_real_module_parses(self):
        r = friday.run_module("system", ["memory"])
        self.assertTrue(r["ok"])
        self.assertIn("total_gb", r["data"])

    def test_missing_module_is_data_not_crash(self):
        r = friday.run_module("no_such_module_xyz", [])
        self.assertFalse(r["ok"])
        self.assertIn("error", r)


class TestRunPower(unittest.TestCase):
    def test_none_verb_returns_none(self):
        self.assertIsNone(friday.run_power(None, dry_run=True))

    def test_disallowed_verb_refused_without_calling(self):
        with mock.patch.object(friday, "run_module",
                               side_effect=AssertionError("must not run")):
            out = friday.run_power("shutdown", dry_run=True)
        self.assertFalse(out["ok"])
        self.assertIn("not allowed", out["error"])

    def test_dry_run_omits_confirm(self):
        calls = []

        def fake(m, argv, timeout=friday.MODULE_TIMEOUT):
            calls.append(argv)
            return {"ok": True, "data": {"dry_run": True, "plan": "lock..."}}
        with mock.patch.object(friday, "run_module", side_effect=fake):
            out = friday.run_power("lock", dry_run=True)
        self.assertEqual(calls[0], ["lock"])       # no --confirm
        self.assertIsNone(out["ok"])
        self.assertTrue(out["dry_run"])

    def test_live_passes_confirm(self):
        calls = []

        def fake(m, argv, timeout=friday.MODULE_TIMEOUT):
            calls.append(argv)
            return {"ok": True, "data": {"confirmed": True}}
        with mock.patch.object(friday, "run_module", side_effect=fake):
            out = friday.run_power("lock", dry_run=False)
        self.assertIn("--confirm", calls[0])
        self.assertTrue(out["ok"])


class TestStatusScene(unittest.TestCase):
    def _status(self, no_mail=True, overrides=None, vault_readable=True):
        store = []
        overrides = overrides or {}

        def fake_emit(ok, data=None, error=None, code=0):
            store.append({"ok": ok, "data": data, "error": error, "code": code})
            raise _Emitted

        def fake_run(module, argv, timeout=friday.MODULE_TIMEOUT):
            if module in overrides:
                return overrides[module]
            if module == "credentials":
                if not vault_readable:
                    return {"ok": True, "data": {"exists": False,
                                                 "vault_readable": False}}
                svc = argv[argv.index("--service") + 1]
                return {"ok": True, "data": {"exists": svc == "icloud_mail"}}
            return STATUS_DEFAULTS.get(module, {"ok": False, "error": "unexpected"})

        with mock.patch.object(friday, "emit", fake_emit), \
                mock.patch.object(friday, "run_module", side_effect=fake_run), \
                mock.patch.object(friday, "mail_glance",
                                  return_value={"icloud": {"unread": 2,
                                                           "inbox_total": 9}}):
            try:
                friday.status(argparse.Namespace(action="status", no_mail=no_mail))
            except _Emitted:
                pass
        return store[0]

    def test_all_ok_assembles_dashboard(self):
        out = self._status()
        self.assertTrue(out["ok"])
        self.assertEqual(out["code"], 0)
        self.assertEqual(set(out["data"]),
                         {"kill_switch", "system", "net", "mail", "vault",
                          "elapsed_sec"})
        self.assertIsNone(out["data"]["mail"])  # --no-mail
        self.assertTrue(out["data"]["vault"]["readable"])
        self.assertEqual(out["data"]["vault"]["services"],
                         {"icloud_mail": True, "gmail": False, "github": False})

    def test_vault_unreadable_session_flagged(self):
        # over an SSH key login DPAPI is locked -> readable False, not "MISSING"
        out = self._status(vault_readable=False)
        self.assertFalse(out["data"]["vault"]["readable"])

    def test_net_failure_degrades_exit2(self):
        out = self._status(overrides={"net": {"ok": False, "error": "down"}})
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], 2)
        self.assertIn("error", out["data"]["net"])

    def test_mail_included_when_enabled(self):
        out = self._status(no_mail=False)
        self.assertIsNotNone(out["data"]["mail"])
        self.assertEqual(out["data"]["mail"]["icloud"]["unread"], 2)

    def test_reports_kill_switch_without_obeying(self):
        # even ENGAGED, status still runs and reports it (read-only scene)
        out = self._status(overrides={"safety": {
            "ok": True, "data": {"engaged": True, "file": "f", "detail": {}}}})
        self.assertTrue(out["data"]["kill_switch"]["engaged"])


class TestRespond(unittest.TestCase):
    """The 5b tripwire: threshold, cooldown, kill-switch, dry-run — run_module
    and the cooldown file mocked/redirected so nothing real is touched."""

    SW = {"failed_logon_count": 10, "window_sec": 3600, "cooldown_sec": 300,
          "defender_any": True}

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hubresp_")
        self.cooldown = Path(self._tmp) / "cool"
        self._prev = {k: os.environ.get(k)
                      for k in ("HUB_RESPOND_COOLDOWN_FILE", "HUB_KILL_FILE")}
        os.environ["HUB_RESPOND_COOLDOWN_FILE"] = str(self.cooldown)
        os.environ.pop("HUB_KILL_FILE", None)

    def tearDown(self):
        for k, v in self._prev.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _respond(self, intruders, dry_run=False):
        store, calls = [], []

        def fake_emit(ok, data=None, error=None, code=0):
            store.append({"ok": ok, "data": data, "error": error, "code": code})
            raise _Emitted

        def fake_run(module, argv, timeout=friday.MODULE_TIMEOUT):
            calls.append((module, argv))
            if module == "security":
                return {"ok": True, "data": intruders}
            if module == "notify":
                return {"ok": True, "data": {}}
            return {"ok": False, "error": "unexpected"}

        with mock.patch.object(friday, "emit", fake_emit), \
                mock.patch.object(friday, "run_module", side_effect=fake_run), \
                mock.patch.object(friday, "load_security_watch", return_value=self.SW):
            try:
                friday.respond(argparse.Namespace(action="respond", dry_run=dry_run))
            except _Emitted:
                pass
        return store[0], [m for m, _ in calls]

    def test_triggered_by_failed_logons_alerts_and_arms_cooldown(self):
        intr = {"failed_logons": {"available": True, "count": 15},
                "defender_detections": {"available": True, "count": 0}}
        out, mods = self._respond(intr)
        self.assertTrue(out["data"]["triggered"])
        self.assertTrue(out["data"]["notified"])
        self.assertIn("notify", mods)
        self.assertTrue(self.cooldown.exists())   # cooldown armed

    def test_below_threshold_no_alert(self):
        intr = {"failed_logons": {"available": True, "count": 3},
                "defender_detections": {"available": True, "count": 0}}
        out, mods = self._respond(intr)
        self.assertFalse(out["data"]["triggered"])
        self.assertNotIn("notify", mods)
        self.assertFalse(self.cooldown.exists())

    def test_defender_detection_triggers(self):
        intr = {"failed_logons": {"available": True, "count": 0},
                "defender_detections": {"available": True, "count": 2}}
        out, mods = self._respond(intr)
        self.assertTrue(out["data"]["triggered"])
        self.assertIn("notify", mods)

    def test_cooldown_suppresses_and_skips_scan(self):
        self.cooldown.write_text(str(time.time()))   # fresh marker
        intr = {"failed_logons": {"available": True, "count": 99}}
        out, mods = self._respond(intr)
        self.assertTrue(out["data"]["cooling_down"])
        self.assertFalse(out["data"]["triggered"])
        self.assertNotIn("security", mods)           # never even scanned

    def test_dry_run_evaluates_but_no_toast_or_marker(self):
        intr = {"failed_logons": {"available": True, "count": 15},
                "defender_detections": {"available": True, "count": 0}}
        out, mods = self._respond(intr, dry_run=True)
        self.assertTrue(out["data"]["triggered"])    # would alert
        self.assertFalse(out["data"]["notified"])
        self.assertNotIn("notify", mods)
        self.assertFalse(self.cooldown.exists())     # no marker written

    def test_kill_switch_refuses_no_scan(self):
        kill = Path(self._tmp) / "kill"
        kill.write_text("{}", encoding="utf-8")
        os.environ["HUB_KILL_FILE"] = str(kill)
        out, mods = self._respond({"failed_logons": {"available": True, "count": 99}})
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], 1)
        self.assertNotIn("security", mods)           # gated before any read


class TestWatch(unittest.TestCase):
    """Scheduled-task registration: pure XML builder + confirm/admin gating,
    with schtasks never actually invoked."""

    SCHTASKS_LIST_READY = (
        "Folder: \\\n"
        "HostName:      L\n"
        "TaskName:      \\RemoteHubSecurityTripwire\n"
        "Next Run Time: N/A\n"
        "Status:        Ready\n"
        "Logon Mode:    Interactive only\n")

    def _watch(self, watch_action, confirm=False, admin=True, schtasks_ret=None):
        store, calls = [], []

        def fake_emit(ok, data=None, error=None, code=0):
            store.append({"ok": ok, "data": data, "error": error, "code": code})
            raise _Emitted

        def fake_schtasks(argv, timeout=friday.SCHTASKS_TIMEOUT):
            calls.append(argv)
            return schtasks_ret

        ns = argparse.Namespace(action="watch", watch_action=watch_action,
                                confirm=confirm)
        with mock.patch.object(friday, "emit", fake_emit), \
                mock.patch.object(friday, "_is_admin", return_value=admin), \
                mock.patch.object(friday, "_schtasks", side_effect=fake_schtasks):
            try:
                friday.watch(ns)
            except _Emitted:
                pass
        return store[0], calls

    def test_build_task_xml_is_wellformed_and_complete(self):
        import xml.dom.minidom as minidom
        xml = friday.build_task_xml(r"C:\py\python.exe", r"C:\repo", "DOM\\user")
        minidom.parseString(xml)  # raises if malformed
        for needle in ("EventID=4625", "Windows Defender/Operational", "1116",
                       "1117", "-m hub.friday respond", "<Hidden>true</Hidden>",
                       "IgnoreNew", "InteractiveToken", "HighestAvailable"):
            self.assertIn(needle, xml)

    def test_install_dry_run_registers_nothing(self):
        out, calls = self._watch("install", confirm=False)
        self.assertTrue(out["data"]["dry_run"])
        self.assertIn("task_xml", out["data"])
        self.assertIn("schtasks_command", out["data"])
        self.assertEqual(calls, [])   # schtasks never invoked

    def test_install_confirm_without_admin_refused(self):
        out, calls = self._watch("install", confirm=True, admin=False)
        self.assertFalse(out["ok"])
        self.assertEqual(out["code"], 1)
        self.assertIn("elevated", out["error"])
        self.assertEqual(calls, [])   # never touched Task Scheduler

    def test_uninstall_dry_run_registers_nothing(self):
        out, calls = self._watch("uninstall", confirm=False)
        self.assertTrue(out["data"]["dry_run"])
        self.assertEqual(calls, [])

    def test_status_registered_parses_state(self):
        ret = SimpleNamespace(returncode=0, stdout=self.SCHTASKS_LIST_READY,
                              stderr="")
        out, calls = self._watch("status", schtasks_ret=ret)
        self.assertTrue(out["data"]["registered"])
        self.assertEqual(out["data"]["state"], "Ready")

    def test_status_not_registered(self):
        ret = SimpleNamespace(returncode=1, stdout="", stderr="ERROR: not found")
        out, calls = self._watch("status", schtasks_ret=ret)
        self.assertFalse(out["data"]["registered"])
        self.assertIsNone(out["data"]["state"])


class TestCLIExitCodes(unittest.TestCase):
    def test_dry_run_trigger_clean_exit0(self):
        env, code = run_cli("friday", "trigger", "chill", "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(env["ok"])
        self.assertTrue(env["data"]["dry_run"])

    def test_goodnight_dry_run_plans_lock_without_acting(self):
        # dry-run must PLAN the lock, never execute it (would lock the machine).
        env, code = run_cli("friday", "trigger", "goodnight", "--dry-run",
                            timeout=30)
        self.assertEqual(code, 0)
        self.assertTrue(env["data"]["dry_run"])
        self.assertEqual(env["data"]["power"]["verb"], "lock")
        self.assertIsNone(env["data"]["power"]["ok"])  # planned, not executed

    def test_unknown_profile_aborts_exit1(self):
        env, code = run_cli("friday", "trigger", "no-such-profile", "--dry-run")
        self.assertEqual(code, 1)
        self.assertFalse(env["ok"])
        self.assertIn("unknown profile", env["error"])

    def test_respond_dry_run_live(self):
        # reads real intruder signals; must be dry-run safe (no toast/marker).
        env, code = run_cli("friday", "respond", "--dry-run", timeout=45)
        self.assertIn(code, (0, 2))
        self.assertTrue(env["data"]["dry_run"])
        for key in ("triggered", "signals", "thresholds"):
            self.assertIn(key, env["data"])

    def test_status_scene_live(self):
        # read-only dashboard against the live machine; exit 0 (or 2 if a read
        # genuinely failed), never 1. Assert the envelope + sections assemble.
        env, code = run_cli("friday", "status", "--no-mail", timeout=45)
        self.assertIn(code, (0, 2))
        for key in ("kill_switch", "system", "net", "vault"):
            self.assertIn(key, env["data"])

    def test_kill_switch_aborts_exit1(self):
        with tempfile.TemporaryDirectory(prefix="hubtest_") as d:
            kill = Path(d) / "KILLSWITCH"
            kill.write_text(json.dumps({"reason": "test"}), encoding="utf-8")
            cli_env = {**os.environ, "HUB_KILL_FILE": str(kill)}
            env, code = run_cli("friday", "trigger", "chill", "--dry-run",
                                env=cli_env)
            self.assertEqual(code, 1)
            self.assertFalse(env["ok"])
            self.assertIn("kill-switch", env["error"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
