"""Unit + CLI tests for hub.clipboard.

The real system clipboard is shared with the user, so set/clear are tested
ONLY with the io helpers mocked — no test ever overwrites the live clipboard.
`get` is read-only (non-destructive) and gets one live envelope smoke test.
"""

from __future__ import annotations

import argparse
import unittest
from unittest import mock

from hub import clipboard as clip
from tests._helpers import assert_envelope, run_cli


class _Emitted(Exception):
    pass


def capture_emit(store):
    def fake(ok, action, data=None, error=None):
        store.append({"ok": ok, "action": action, "data": data, "error": error})
        raise _Emitted
    return fake


def g(max_chars=4096, reveal=False):
    return argparse.Namespace(action="get", max_chars=max_chars, reveal=reveal)


class TestSensitiveHeuristic(unittest.TestCase):
    def test_keyword_and_token_and_pem_flagged(self):
        self.assertTrue(clip._looks_sensitive("my password is hunter2"))
        self.assertTrue(clip._looks_sensitive("sk-Abc123XyzDef456Ghi789Jkl"))
        self.assertTrue(clip._looks_sensitive(
            "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END"))

    def test_ordinary_text_not_flagged(self):
        self.assertFalse(clip._looks_sensitive("hello from friday here"))
        self.assertFalse(clip._looks_sensitive("elephant"))
        self.assertFalse(clip._looks_sensitive(""))


class TestGetWithholding(unittest.TestCase):
    def _get(self, clip_text, **over):
        store = []
        with mock.patch.object(clip, "emit", capture_emit(store)), \
                mock.patch.object(clip, "_get_text", return_value=clip_text):
            try:
                clip.do_get(g(**over))
            except _Emitted:
                pass
        return store[0]

    def test_sensitive_withheld_by_default(self):
        out = self._get("api_key=SUPERSECRETVALUE123")
        self.assertTrue(out["data"]["looks_sensitive"])
        self.assertTrue(out["data"]["withheld"])
        self.assertIsNone(out["data"]["content"])

    def test_reveal_returns_sensitive(self):
        out = self._get("api_key=SUPERSECRETVALUE123", reveal=True)
        self.assertFalse(out["data"]["withheld"])
        self.assertIn("SUPERSECRET", out["data"]["content"])

    def test_ordinary_returned(self):
        out = self._get("just some notes here")
        self.assertFalse(out["data"]["withheld"])
        self.assertEqual(out["data"]["content"], "just some notes here")

    def test_empty_clipboard(self):
        out = self._get(None)
        self.assertFalse(out["data"]["available"])

    def test_truncation_flagged(self):
        out = self._get("x" * 100, max_chars=10)
        self.assertTrue(out["data"]["truncated"])
        self.assertEqual(len(out["data"]["content"]), 10)


class TestSetClearGate(unittest.TestCase):
    def _run(self, handler, ns, io_name):
        store = []
        with mock.patch.object(clip, "emit", capture_emit(store)), \
                mock.patch.object(clip, io_name) as io:
            try:
                handler(ns)
            except _Emitted:
                pass
        return store[0], io

    def test_set_dry_run_does_not_write(self):
        ns = argparse.Namespace(action="set", text="hi", confirm=False)
        out, io = self._run(clip.do_set, ns, "_set_text")
        self.assertTrue(out["data"]["dry_run"])
        io.assert_not_called()

    def test_set_confirm_writes(self):
        ns = argparse.Namespace(action="set", text="hi there", confirm=True)
        out, io = self._run(clip.do_set, ns, "_set_text")
        self.assertTrue(out["data"]["confirmed"])
        io.assert_called_once_with("hi there")

    def test_set_over_cap_refused(self):
        ns = argparse.Namespace(action="set", text="x" * (clip.SET_MAX_CHARS + 1),
                                confirm=True)
        out, io = self._run(clip.do_set, ns, "_set_text")
        self.assertFalse(out["ok"])
        io.assert_not_called()

    def test_clear_dry_run_then_confirm(self):
        out, io = self._run(clip.do_clear,
                            argparse.Namespace(action="clear", confirm=False),
                            "_empty")
        self.assertTrue(out["data"]["dry_run"])
        io.assert_not_called()

        out, io = self._run(clip.do_clear,
                            argparse.Namespace(action="clear", confirm=True),
                            "_empty")
        self.assertTrue(out["data"]["confirmed"])
        io.assert_called_once()


class TestClipboardCLI(unittest.TestCase):
    def test_get_envelope_read_only(self):
        env, code = run_cli("clipboard", "get", "--max-chars", "40")
        assert_envelope(env, code)
        self.assertTrue(env["ok"])
        self.assertIn("available", env["data"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
