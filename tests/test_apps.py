"""Unit + CLI tests for hub.apps — window control and the guarded kill.

The load-bearing security surface is do_kill's fail-closed owner scoping and
resolve_one_window's refuse-on-ambiguity. Those are exercised with psutil /
enum_windows mocked so no real process is ever touched. Live CLI tests only
use paths that refuse before acting (guarded names) or match nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import apps
from tests._helpers import assert_envelope, run_cli


class _Emitted(Exception):
    """Stand-in for emit()'s sys.exit: unwinds the handler after it emits."""


def capture_emit(store):
    def fake(ok, action, data=None, error=None):
        store.append({"ok": ok, "action": action, "data": data, "error": error})
        raise _Emitted
    return fake


def win(hwnd=1, pid=100, process="app.exe", title="A Window",
        minimized=False, foreground=False):
    return apps.WindowInfo(hwnd, pid, process, title, minimized, foreground)


class FakeProc:
    def __init__(self, pid, name, username):
        self.pid = pid
        self.info = {"pid": pid, "name": name, "username": username}


class TestKillGuardSet(unittest.TestCase):
    def test_critical_names_present(self):
        for name in ("system", "lsass.exe", "svchost.exe", "winlogon.exe",
                     "csrss.exe", "services.exe", "explorer.exe",
                     "python.exe", "claude.exe", "code.exe", "taskmgr.exe"):
            self.assertIn(name, apps.CRITICAL_KILL_GUARD)


class TestKillOwnerScoping(unittest.TestCase):
    """The fail-closed heart: identity required, positive same-user match only."""

    def _run_kill(self, procs, me="TESTDOM\\me", me_error=False):
        store = []
        args = argparse.Namespace(name="target.exe", dry_run=True,
                                  timeout=5.0, action="kill")
        proc_mock = mock.MagicMock()
        if me_error:
            proc_mock.return_value.username.side_effect = apps.psutil.Error("x")
        else:
            proc_mock.return_value.username.return_value = me
        with mock.patch.object(apps, "emit", capture_emit(store)), \
                mock.patch.object(apps.psutil, "Process", proc_mock), \
                mock.patch.object(apps.psutil, "process_iter", return_value=procs):
            with self.assertRaises(_Emitted):
                apps.do_kill(args)
        return store[0]

    def test_refuses_when_identity_unresolvable(self):
        out = self._run_kill([], me_error=True)
        self.assertFalse(out["ok"])
        self.assertIn("cannot resolve", out["error"].lower())

    def test_targets_only_same_user(self):
        procs = [
            FakeProc(101, "target.exe", "TESTDOM\\me"),      # mine -> target
            FakeProc(102, "target.exe", "TESTDOM\\other"),   # other user -> skip
            FakeProc(103, "target.exe", None),               # unreadable -> skip
            FakeProc(104, "different.exe", "TESTDOM\\me"),    # wrong name -> skip
        ]
        out = self._run_kill(procs)
        self.assertTrue(out["ok"])
        self.assertTrue(out["data"]["dry_run"])
        self.assertEqual(out["data"]["pids"], [101])

    def test_no_matches_is_clean_zero(self):
        out = self._run_kill([FakeProc(200, "somethingelse.exe", "TESTDOM\\me")])
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"]["pids"], [])

    def test_guard_refuses_before_scanning(self):
        store = []
        args = argparse.Namespace(name="LSASS.EXE", dry_run=True,
                                  timeout=5.0, action="kill")
        # process_iter must never be consulted once the guard trips.
        with mock.patch.object(apps, "emit", capture_emit(store)), \
                mock.patch.object(apps.psutil, "process_iter",
                                  side_effect=AssertionError("scanned!")):
            with self.assertRaises(_Emitted):
                apps.do_kill(args)
        self.assertFalse(store[0]["ok"])
        self.assertIn("protected", store[0]["error"].lower())


class TestResolveAmbiguity(unittest.TestCase):
    def _resolve(self, matches, **arg_over):
        store = []
        args = argparse.Namespace(hwnd=None, pid=None, title="win",
                                  action="focus")
        args.__dict__.update(arg_over)
        with mock.patch.object(apps, "emit", capture_emit(store)), \
                mock.patch.object(apps, "enum_windows", return_value=matches):
            try:
                result = apps.resolve_one_window(args)
            except _Emitted:
                result = None
        return result, store

    def test_single_match_returned(self):
        result, store = self._resolve([win(hwnd=7)])
        self.assertEqual(store, [])
        self.assertEqual(result.hwnd, 7)

    def test_no_match_emits_error(self):
        result, store = self._resolve([])
        self.assertIsNone(result)
        self.assertFalse(store[0]["ok"])
        self.assertIn("no window matched", store[0]["error"])

    def test_ambiguous_refused_with_candidates(self):
        result, store = self._resolve([win(hwnd=1), win(hwnd=2)])
        self.assertIsNone(result)
        self.assertFalse(store[0]["ok"])
        self.assertIn("disambiguate", store[0]["error"])
        self.assertEqual(len(store[0]["data"]), 2)


class TestKillCLILive(unittest.TestCase):
    """Live CLI, but only paths that refuse before acting or match nothing."""

    def test_guarded_name_refused(self):
        env, code = run_cli("apps", "kill", "--name", "explorer.exe")
        assert_envelope(env, code)
        self.assertFalse(env["ok"])
        self.assertIn("protected", env["error"].lower())

    def test_guard_is_case_insensitive(self):
        env, code = run_cli("apps", "kill", "--name", "Explorer.EXE")
        assert_envelope(env, code)
        self.assertFalse(env["ok"])

    def test_nonexistent_name_matches_zero(self):
        env, code = run_cli("apps", "kill", "--name",
                            "no-such-proc-xyz.exe", "--dry-run")
        assert_envelope(env, code)
        self.assertTrue(env["ok"])
        self.assertEqual(env["data"]["matched"], 0)


class TestKillSwitchRefusal(unittest.TestCase):
    def test_apps_refuses_when_kill_switch_engaged(self):
        with tempfile.TemporaryDirectory(prefix="hubtest_") as d:
            kill = Path(d) / "KILLSWITCH"
            kill.write_text(json.dumps({"reason": "test"}), encoding="utf-8")
            env = {**os.environ, "HUB_KILL_FILE": str(kill)}
            got, code = run_cli("apps", "list", env=env)
            assert_envelope(got, code)
            self.assertFalse(got["ok"])
            self.assertIn("kill-switch", got["error"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
