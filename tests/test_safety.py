"""Unit tests for hub.safety — the substrate every module trusts.

Isolated from the real kill-switch: each test points HUB_KILL_FILE at a temp
path, so the production hub/KILLSWITCH is never touched.
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

from hub import safety


class KillFileIsolation(unittest.TestCase):
    """Base class: redirect the kill-switch to a private temp file per test."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hubtest_")
        self._kill = Path(self._tmp) / "KILLSWITCH"
        self._prev = os.environ.get("HUB_KILL_FILE")
        os.environ["HUB_KILL_FILE"] = str(self._kill)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("HUB_KILL_FILE", None)
        else:
            os.environ["HUB_KILL_FILE"] = self._prev
        try:
            if self._kill.exists():
                self._kill.unlink()
            os.rmdir(self._tmp)
        except OSError:
            pass


class TestKillSwitch(KillFileIsolation):
    def test_engage_disengage_roundtrip(self):
        self.assertFalse(safety.kill_engaged())
        safety.engage_kill("because")
        self.assertTrue(safety.kill_engaged())
        st = safety.kill_status()
        self.assertTrue(st["engaged"])
        self.assertEqual(st["detail"]["reason"], "because")
        out = safety.disengage_kill()
        self.assertTrue(out["was_engaged"])
        self.assertFalse(safety.kill_engaged())

    def test_disengage_when_absent_is_safe(self):
        out = safety.disengage_kill()
        self.assertFalse(out["was_engaged"])

    def test_assert_alive_raises_when_engaged(self):
        safety.engage_kill("stop")
        with self.assertRaises(safety.KillSwitchEngaged):
            safety.assert_alive("unit-test")

    def test_assert_alive_silent_when_clear(self):
        self.assertIsNone(safety.assert_alive("ok"))

    def test_unreadable_kill_file_still_counts_as_engaged(self):
        # A garbage (non-JSON) marker must still engage — fail closed.
        self._kill.write_text("not json {{{", encoding="utf-8")
        self.assertTrue(safety.kill_engaged())
        st = safety.kill_status()
        self.assertTrue(st["engaged"])
        self.assertEqual(st["detail"], "unreadable (still counts as engaged)")

    def test_env_override_is_respected(self):
        # The isolation itself proves env override works, but assert explicitly.
        self.assertEqual(safety.kill_file(), self._kill)


class TestDeadline(KillFileIsolation):
    def test_nonpositive_rejected(self):
        with self.assertRaises(ValueError):
            safety.Deadline(0)
        with self.assertRaises(ValueError):
            safety.Deadline(-1)

    def test_remaining_clamps_and_expires(self):
        dl = safety.Deadline(0.05)
        self.assertGreater(dl.remaining(), 0)
        self.assertFalse(dl.expired)
        time.sleep(0.08)
        self.assertEqual(dl.remaining(), 0.0)
        self.assertTrue(dl.expired)

    def test_check_raises_after_expiry(self):
        dl = safety.Deadline(0.02)
        time.sleep(0.05)
        with self.assertRaises(safety.DeadlineExceeded):
            dl.check("phase")

    def test_sleep_never_oversleeps_budget(self):
        dl = safety.Deadline(0.1)
        t0 = time.monotonic()
        dl.sleep(5.0)  # asks for 5s, must be clamped to ~remaining
        self.assertLess(time.monotonic() - t0, 1.0)


class TestDeadlineDecorator(KillFileIsolation):
    def test_fast_path_returns_value(self):
        @safety.deadline(2.0)
        def quick():
            return 7
        self.assertEqual(quick(), 7)

    def test_slow_path_raises_promptly(self):
        @safety.deadline(0.2)
        def slow():
            time.sleep(5)
        t0 = time.monotonic()
        with self.assertRaises(safety.DeadlineExceeded):
            slow()
        self.assertLess(time.monotonic() - t0, 1.5)

    def test_callee_exception_propagates(self):
        @safety.deadline(2.0)
        def boom():
            raise ValueError("inner")
        with self.assertRaises(ValueError):
            boom()

    def test_kill_checked_on_entry(self):
        @safety.deadline(2.0)
        def guarded():
            return "ran"
        safety.engage_kill("halt")
        with self.assertRaises(safety.KillSwitchEngaged):
            guarded()


class TestRetry(KillFileIsolation):
    def test_recovers_on_later_attempt(self):
        calls = {"n": 0}

        @safety.retry(attempts=5, base_delay=0.001, max_delay=0.002, total_timeout=5)
        def flaky():
            calls["n"] += 1
            if calls["n"] < 3:
                raise IOError("transient")
            return "ok"
        self.assertEqual(flaky(), "ok")
        self.assertEqual(calls["n"], 3)

    def test_exhaustion_raises_with_cause(self):
        @safety.retry(attempts=2, base_delay=0.001, total_timeout=5)
        def hopeless():
            raise IOError("permanent")
        with self.assertRaises(safety.RetriesExhausted) as ctx:
            hopeless()
        self.assertIsInstance(ctx.exception.__cause__, IOError)

    def test_never_retry_propagates_immediately(self):
        calls = {"n": 0}

        @safety.retry(attempts=5, base_delay=0.001, total_timeout=5,
                      retry_on=(Exception,))
        def kill_mid():
            calls["n"] += 1
            raise safety.KillSwitchEngaged("switched")
        with self.assertRaises(safety.KillSwitchEngaged):
            kill_mid()
        self.assertEqual(calls["n"], 1, "safety exception must not be retried")

    def test_unlisted_exception_not_retried(self):
        calls = {"n": 0}

        @safety.retry(attempts=5, base_delay=0.001, total_timeout=5,
                      retry_on=(IOError,))
        def wrong_type():
            calls["n"] += 1
            raise ValueError("not in retry_on")
        with self.assertRaises(ValueError):
            wrong_type()
        self.assertEqual(calls["n"], 1)

    def test_kill_switch_stops_retry_loop(self):
        @safety.retry(attempts=5, base_delay=0.001, total_timeout=5)
        def always_fails():
            raise IOError("nope")
        safety.engage_kill("halt")
        with self.assertRaises(safety.KillSwitchEngaged):
            always_fails()


class TestWaitUntil(KillFileIsolation):
    def test_returns_truthy_when_predicate_flips(self):
        state = {"n": 0}

        def pred():
            state["n"] += 1
            return state["n"] >= 3
        self.assertTrue(safety.wait_until(pred, timeout=5, poll=0.001))

    def test_timeout_returns_falsy_not_raise(self):
        self.assertFalse(safety.wait_until(lambda: False, timeout=0.1, poll=0.02))

    def test_respects_kill_switch(self):
        safety.engage_kill("halt")
        with self.assertRaises(safety.KillSwitchEngaged):
            safety.wait_until(lambda: False, timeout=1, poll=0.05)

    def test_kill_ignored_when_disabled(self):
        safety.engage_kill("halt")
        # respect_kill=False must poll regardless of the switch.
        self.assertTrue(
            safety.wait_until(lambda: True, timeout=1, respect_kill=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
