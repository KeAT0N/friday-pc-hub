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
import unittest
from pathlib import Path
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


class TestStatusScene(unittest.TestCase):
    def _status(self, no_mail=True, overrides=None):
        store = []
        overrides = overrides or {}

        def fake_emit(ok, data=None, error=None, code=0):
            store.append({"ok": ok, "data": data, "error": error, "code": code})
            raise _Emitted

        def fake_run(module, argv, timeout=friday.MODULE_TIMEOUT):
            if module in overrides:
                return overrides[module]
            if module == "credentials":
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
        self.assertEqual(out["data"]["vault"],
                         {"icloud_mail": True, "gmail": False, "github": False})

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


class TestCLIExitCodes(unittest.TestCase):
    def test_dry_run_trigger_clean_exit0(self):
        env, code = run_cli("friday", "trigger", "chill", "--dry-run")
        self.assertEqual(code, 0)
        self.assertTrue(env["ok"])
        self.assertTrue(env["data"]["dry_run"])

    def test_unknown_profile_aborts_exit1(self):
        env, code = run_cli("friday", "trigger", "no-such-profile", "--dry-run")
        self.assertEqual(code, 1)
        self.assertFalse(env["ok"])
        self.assertIn("unknown profile", env["error"])

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
