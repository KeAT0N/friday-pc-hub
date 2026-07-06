"""Unit + CLI tests for hub.power — guarded session control.

The confirm-gate is the whole safety story, so it is tested exhaustively with
the OS-effect helpers mocked: without --confirm nothing is called (dry-run
preview); with --confirm exactly the right effect is called with the right
args. No test ever locks, sleeps, or shuts down the real machine.
"""

from __future__ import annotations

import argparse
import unittest
from unittest import mock

from hub import power
from tests._helpers import assert_envelope, run_cli


class _Emitted(Exception):
    pass


def capture_emit(store):
    def fake(ok, action, data=None, error=None):
        store.append({"ok": ok, "action": action, "data": data, "error": error})
        raise _Emitted
    return fake


def args(action, confirm=False, delay=60):
    return argparse.Namespace(action=action, confirm=confirm, delay=delay)


ACTING_VERBS = ["lock", "monitor-off", "sleep", "hibernate", "shutdown", "restart"]


class TestConfirmGate(unittest.TestCase):
    def _dispatch(self, ns):
        store = []
        with mock.patch.object(power, "emit", capture_emit(store)), \
                mock.patch.object(power, "_lock") as m_lock, \
                mock.patch.object(power, "_set_monitor_off") as m_mon, \
                mock.patch.object(power, "_suspend") as m_susp, \
                mock.patch.object(power, "_shutdown") as m_shut:
            try:
                power.dispatch(ns)
            except _Emitted:
                pass
        return store[0], dict(lock=m_lock, mon=m_mon, susp=m_susp, shut=m_shut)

    def test_dry_run_default_calls_no_effect(self):
        for verb in ACTING_VERBS:
            with self.subTest(verb=verb):
                out, effects = self._dispatch(args(verb, confirm=False))
                self.assertTrue(out["ok"])
                self.assertTrue(out["data"]["dry_run"])
                self.assertTrue(out["data"]["confirm_required"])
                for m in effects.values():
                    m.assert_not_called()

    def test_confirm_calls_exactly_the_right_effect(self):
        cases = {
            "lock": ("lock", ()),
            "monitor-off": ("mon", ()),
            "sleep": ("susp", (False,)),
            "hibernate": ("susp", (True,)),
        }
        for verb, (key, expected_args) in cases.items():
            with self.subTest(verb=verb):
                out, effects = self._dispatch(args(verb, confirm=True))
                self.assertTrue(out["ok"])
                self.assertTrue(out["data"]["confirmed"])
                effects[key].assert_called_once()
                if expected_args:
                    self.assertEqual(effects[key].call_args.args, expected_args)

    def test_confirm_shutdown_and_restart_pass_delay(self):
        out, effects = self._dispatch(args("shutdown", confirm=True, delay=30))
        effects["shut"].assert_called_once_with(False, 30)
        self.assertEqual(out["data"]["delay_sec"], 30)

        out, effects = self._dispatch(args("restart", confirm=True, delay=5))
        effects["shut"].assert_called_once_with(True, 5)


class TestStatusAndCancel(unittest.TestCase):
    def test_status_live_shape(self):
        d = power._power_status()
        self.assertIn(d["ac_power"], ("ac", "battery", "unknown"))
        self.assertIn("battery_percent", d)
        self.assertIn("active_scheme", d)

    def test_cancel_dispatch_reports_nothing_pending(self):
        store = []
        with mock.patch.object(power, "emit", capture_emit(store)), \
                mock.patch.object(power, "_cancel_shutdown",
                                  return_value=(False, "nothing")):
            try:
                power.dispatch(args("cancel"))
            except _Emitted:
                pass
        self.assertTrue(store[0]["ok"])
        self.assertFalse(store[0]["data"]["cancelled"])


class TestPowerCLI(unittest.TestCase):
    def test_status_envelope(self):
        env, code = run_cli("power", "status")
        assert_envelope(env, code)
        self.assertTrue(env["ok"])
        self.assertIn("ac_power", env["data"])

    def test_lock_dry_run_envelope_does_not_act(self):
        # No --confirm: must be a preview, exit 0, machine untouched.
        env, code = run_cli("power", "lock")
        assert_envelope(env, code)
        self.assertTrue(env["data"]["dry_run"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
