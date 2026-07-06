"""Unit + CLI tests for hub.push (ntfy phone notifications).

No test makes a real network call: post() / load_config are mocked. The topic
is a secret, so tests never read or overwrite the real hub/.push.json (they
patch CONFIG_PATH / use env).
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hub import push
from tests._helpers import assert_envelope, run_cli


class _Emitted(Exception):
    pass


def capture_emit(store):
    def fake(ok, action, data=None, error=None):
        store.append({"ok": ok, "action": action, "data": data, "error": error})
        raise _Emitted
    return fake


def _no_env():
    ctx = mock.patch.dict(os.environ, {}, clear=False)
    ctx.start()
    os.environ.pop("HUB_NTFY_TOPIC", None)
    os.environ.pop("HUB_NTFY_SERVER", None)
    return ctx


class TestConfig(unittest.TestCase):
    def test_env_override_wins(self):
        with mock.patch.dict(os.environ,
                             {"HUB_NTFY_TOPIC": "envtopic",
                              "HUB_NTFY_SERVER": "https://x"}):
            cfg = push.load_config()
        self.assertEqual(cfg["topic"], "envtopic")
        self.assertEqual(cfg["server"], "https://x")

    def test_file_used_when_no_env(self):
        ctx = _no_env()
        try:
            with tempfile.TemporaryDirectory() as d:
                p = Path(d) / "c.json"
                p.write_text(json.dumps({"server": "https://s", "topic": "ftopic"}))
                with mock.patch.object(push, "CONFIG_PATH", p):
                    cfg = push.load_config()
            self.assertEqual(cfg["topic"], "ftopic")
            self.assertEqual(cfg["server"], "https://s")
        finally:
            ctx.stop()

    def test_unconfigured_topic_is_none(self):
        ctx = _no_env()
        try:
            with tempfile.TemporaryDirectory() as d:
                with mock.patch.object(push, "CONFIG_PATH", Path(d) / "absent.json"):
                    cfg = push.load_config()
            self.assertIsNone(cfg["topic"])
            self.assertEqual(cfg["server"], push.DEFAULT_SERVER)
        finally:
            ctx.stop()


class TestHelpers(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(push._mask("remotehub-abcdef"), "remote…")
        self.assertEqual(push._mask("abc"), "set")
        self.assertIsNone(push._mask(None))

    def test_ascii_header_strips_control_and_nonascii(self):
        out = push._ascii_header("a\x00b\x07 café", 100)
        self.assertTrue(out.isascii())
        self.assertNotIn("\x00", out)
        self.assertNotIn("\x07", out)

    def test_ascii_header_clamps(self):
        self.assertEqual(len(push._ascii_header("x" * 500, 10)), 10)


class TestPost(unittest.TestCase):
    def test_builds_request_url_and_sanitized_headers(self):
        captured = {}

        class FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["title"] = req.headers.get("Title")
            captured["priority"] = req.headers.get("Priority")
            return FakeResp()

        with mock.patch.object(push.urllib.request, "urlopen", fake_urlopen):
            status = push.post({"server": "https://ntfy.sh/", "topic": "t"},
                               "Ti\x07tle", "body", "urgent", "warn")
        self.assertEqual(status, 200)
        self.assertEqual(captured["url"], "https://ntfy.sh/t")
        self.assertNotIn("\x07", captured["title"])       # header sanitized
        self.assertEqual(captured["priority"], "urgent")


class TestSend(unittest.TestCase):
    def _send(self, cfg, **over):
        store = []
        ns = argparse.Namespace(title="t", message="m", priority="default",
                                tags="")
        ns.__dict__.update(over)
        with mock.patch.object(push, "emit", capture_emit(store)), \
                mock.patch.object(push, "load_config", return_value=cfg):
            with mock.patch.object(push, "post", return_value=200) as p:
                try:
                    push.do_send(ns)
                except _Emitted:
                    pass
        return store[0], p

    def test_inert_without_topic(self):
        out, p = self._send({"server": push.DEFAULT_SERVER, "topic": None})
        self.assertFalse(out["ok"])
        self.assertIn("inert", out["error"])
        p.assert_not_called()

    def test_posts_when_configured(self):
        out, p = self._send({"server": push.DEFAULT_SERVER, "topic": "t"})
        self.assertTrue(out["ok"])
        self.assertEqual(out["data"]["http_status"], 200)
        p.assert_called_once()


class TestPushCLI(unittest.TestCase):
    def test_status_envelope_read_only(self):
        env, code = run_cli("push", "status")   # reads config only, no network
        assert_envelope(env, code)
        self.assertTrue(env["ok"])
        self.assertIn("configured", env["data"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
