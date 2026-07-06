"""Kill-switch coverage audit — the whole-hub safety invariant in one place.

With the switch engaged, EVERY guarded module CLI must refuse (ok:false + a
"kill-switch" error, exit 1) before doing its work. Two documented exemptions:
  * hub/safety kill-status manages/reads the switch — it must answer while
    engaged, so it is not in the guarded set.
  * friday `status` is a read-only aggregator that REPORTS the switch rather
    than obeying it (so you can diagnose a disarmed hub); it must still emit a
    dashboard with kill_switch.engaged = true.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from tests._helpers import assert_envelope, run_cli

# (module, representative subcommand that routes through assert_alive). Chosen to
# be cheap and to refuse fast (the gate fires before any real work).
GUARDED = [
    ("apps", ["list"]),
    ("system", ["memory"]),
    ("files", ["roots"]),
    ("credentials", ["providers"]),
    ("notify", ["send", "--title", "x", "--message", "y"]),
    ("power", ["status"]),
    ("net", ["adapters"]),
    ("clipboard", ["get"]),
    ("media", ["keys"]),
    ("window", ["list"]),
    ("screen", ["displays"]),
    ("security", ["audit"]),
    ("mail", ["count"]),
]


class KillSwitchAudit(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="hubks_")
        kill = Path(self._tmp) / "KILLSWITCH"
        kill.write_text(json.dumps({"reason": "audit"}), encoding="utf-8")
        self.env = {**os.environ, "HUB_KILL_FILE": str(kill)}

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_every_guarded_module_refuses(self):
        for module, argv in GUARDED:
            with self.subTest(module=module, argv=argv):
                env, code = run_cli(module, *argv, env=self.env, timeout=45)
                assert_envelope(env, code)          # ok:false must exit 1
                self.assertFalse(env["ok"], f"{module} did not refuse")
                self.assertIn("kill-switch", (env["error"] or "").lower(),
                              f"{module} refused for the wrong reason")

    def test_friday_respond_refuses(self):
        env, code = run_cli("friday", "respond", "--dry-run",
                            env=self.env, timeout=30)
        self.assertFalse(env["ok"])
        self.assertEqual(code, 1)
        self.assertIn("kill-switch", (env["error"] or "").lower())

    def test_friday_status_reports_switch_without_obeying(self):
        # documented exemption: read-only aggregator reports the switch state.
        env, code = run_cli("friday", "status", "--no-mail",
                            env=self.env, timeout=45)
        self.assertTrue(env["data"]["kill_switch"]["engaged"])

    def test_safety_kill_status_answers_while_engaged(self):
        # the switch's own query must work when engaged (not in the guarded set).
        env, code = run_cli("safety", "kill", "status", env=self.env)
        self.assertTrue(env["ok"])
        self.assertTrue(env["data"]["engaged"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
