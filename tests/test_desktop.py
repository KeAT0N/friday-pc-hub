"""Unit tests for hub.desktop — the interactive-session bridge.

The bridge only activates in a NON-interactive (SSH) session, which the test
runner isn't, so the SSH side is tested with schtasks / session detection
mocked. The worker (task side) is tested directly with the queue paths
redirected to temp files.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from hub import desktop
from tests._helpers import assert_envelope, run_cli


class _Emitted(Exception):
    pass


def capture_emit(store):
    def fake(ok, action, data=None, error=None):
        store.append({"ok": ok, "action": action, "data": data, "error": error})
        raise _Emitted
    return fake


class TestSessionProbe(unittest.TestCase):
    def test_returns_bool_and_interactive_here(self):
        # the test runner is the interactive console session
        self.assertIs(desktop.in_interactive_session(), True)


class TestTaskXml(unittest.TestCase):
    def test_wellformed_and_complete(self):
        import xml.dom.minidom as minidom
        xml = desktop.build_task_xml(r"C:\py.exe", r"C:\repo", "DOM\\u")
        minidom.parseString(xml)
        for needle in ("hub.desktop worker", "InteractiveToken", "LeastPrivilege",
                       "Queue", "<Hidden>true</Hidden>", "<Triggers />"):
            self.assertIn(needle, xml)


class TestWorker(unittest.TestCase):
    def test_runs_queued_command_and_writes_result(self):
        with tempfile.TemporaryDirectory() as d:
            req = Path(d) / "req.json"
            res = Path(d) / "res.json"
            req.write_text(json.dumps({"id": "abc", "module": "net",
                                       "argv": ["adapters"]}))
            fake = SimpleNamespace(stdout='{"ok": true, "action": "adapters"}',
                                   returncode=0)
            with mock.patch.object(desktop, "REQUEST_PATH", req), \
                    mock.patch.object(desktop, "RESULT_PATH", res), \
                    mock.patch.object(desktop, "_run_local", return_value=fake) as rl:
                desktop.worker()
            rl.assert_called_once_with("net", ["adapters"])
            out = json.loads(res.read_text())
            self.assertEqual(out["id"], "abc")
            self.assertEqual(out["code"], 0)
            self.assertIn("adapters", out["stdout"])

    def test_missing_request_is_noop(self):
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(desktop, "REQUEST_PATH", Path(d) / "nope.json"):
                desktop.worker()   # must not raise


class TestEnsureDesktop(unittest.TestCase):
    def test_noop_when_interactive(self):
        with mock.patch.object(desktop, "in_interactive_session", return_value=True), \
                mock.patch.object(desktop, "bridge") as br:
            desktop.ensure_desktop("apps", ["list"])
            br.assert_not_called()

    def test_bridges_when_not_interactive(self):
        with mock.patch.object(desktop, "in_interactive_session", return_value=False), \
                mock.patch.object(desktop, "bridge") as br:
            desktop.ensure_desktop("apps", ["open", "spotify"])
            br.assert_called_once_with("apps", ["open", "spotify"])


class TestBridgeFailPaths(unittest.TestCase):
    def test_task_not_installed_errors_clearly(self):
        store = []
        with tempfile.TemporaryDirectory() as d:
            with mock.patch.object(desktop, "REQUEST_PATH", Path(d) / "req.json"), \
                    mock.patch.object(desktop, "RESULT_PATH", Path(d) / "res.json"), \
                    mock.patch.object(desktop, "emit", capture_emit(store)), \
                    mock.patch.object(desktop, "_schtasks",
                                      return_value=SimpleNamespace(returncode=1,
                                                                  stderr="not found")):
                with self.assertRaises(_Emitted):
                    desktop.bridge("apps", ["open", "spotify"])
        self.assertFalse(store[0]["ok"])
        self.assertIn("install", store[0]["error"])


class TestDesktopCLI(unittest.TestCase):
    def test_status_envelope(self):
        env, code = run_cli("desktop", "status")
        assert_envelope(env, code)
        self.assertIn("this_session_interactive", env["data"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
