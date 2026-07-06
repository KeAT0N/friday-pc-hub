"""Cross-module contract test: every hub module emits exactly one valid
ASCII envelope on stdout, and its exit code mirrors `ok`.

This is the safety net that keeps all modules honest as the hub grows. It
shells out to each module CLI with a safe, read-only subcommand (notify is
exercised through its kill-switch refusal path so no toast is shown).

friday is intentionally excluded here: it uses a richer exit-code scheme
(0 clean / 1 aborted / 2 degraded) and gets its own test module.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests._helpers import REPO_ROOT, assert_envelope, run_cli

# (module, args) pairs using only read-only / non-mutating subcommands.
SAFE_INVOCATIONS = [
    ("safety", ["kill", "status"]),
    ("safety", ["selftest"]),
    ("system", ["cpu", "--sample", "0.1"]),
    ("system", ["memory"]),
    ("system", ["disk"]),
    ("apps", ["list"]),
    ("apps", ["query", "--name", "definitely-no-such-process-xyz"]),
    ("credentials", ["providers"]),
    ("credentials", ["check", "--service", "no-such-service-xyz"]),
    ("files", ["roots"]),
    ("power", ["status"]),
    ("net", ["adapters"]),
]


class TestEnvelopeContract(unittest.TestCase):
    def test_safe_invocations_are_valid_envelopes(self):
        for module, args in SAFE_INVOCATIONS:
            with self.subTest(module=module, args=args):
                env_obj, code = run_cli(module, *args)
                assert_envelope(env_obj, code)

    def test_stdout_is_pure_ascii(self):
        """The ensure_ascii contract: raw stdout bytes must be ASCII-only."""
        for module, args in SAFE_INVOCATIONS:
            with self.subTest(module=module, args=args):
                proc = subprocess.run(
                    [sys.executable, "-m", f"hub.{module}", *args],
                    cwd=REPO_ROOT, capture_output=True, timeout=30,
                )
                line = proc.stdout.strip()
                self.assertTrue(line, f"hub.{module} emitted nothing")
                # Raw bytes must decode as ASCII with nothing above 0x7F.
                try:
                    line.decode("ascii")
                except UnicodeDecodeError as e:
                    self.fail(f"hub.{module} {args} emitted non-ASCII stdout: {e}")

    def test_notify_kill_switch_refusal_envelope(self):
        """notify has only a mutating verb; prove its envelope via fail-closed."""
        with tempfile.TemporaryDirectory(prefix="hubtest_") as d:
            kill = Path(d) / "KILLSWITCH"
            kill.write_text(json.dumps({"reason": "test"}), encoding="utf-8")
            env = {**os.environ, "HUB_KILL_FILE": str(kill)}
            env_obj, code = run_cli(
                "notify", "send", "--title", "x", "--message", "y", env=env)
            assert_envelope(env_obj, code)
            self.assertFalse(env_obj["ok"])
            self.assertIn("kill-switch", env_obj["error"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
