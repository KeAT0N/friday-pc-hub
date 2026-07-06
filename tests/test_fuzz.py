"""Envelope contract fuzz — hostile input must not break the JSON contract.

Two guarantees:
  * ensure_ascii holds end-to-end: even when a module echoes tricky unicode
    (BMP + astral) or control chars back into its envelope, raw stdout stays
    pure ASCII and json.loads round-trips the original text.
  * Documented input hygiene: notify._sanitize strips control chars + clamps;
    mail._valid_addr rejects header-injection / multi-recipient smuggling.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from hub import mail, notify
from tests._helpers import REPO_ROOT


def raw_stdout(module, *args, env=None):
    proc = subprocess.run(
        [sys.executable, "-m", f"hub.{module}", *args],
        cwd=REPO_ROOT, capture_output=True, timeout=30,
        env=env, stdin=subprocess.DEVNULL)
    return proc.stdout


class TestAsciiRoundTrip(unittest.TestCase):
    def _assert_ascii_and_parse(self, raw: bytes) -> dict:
        raw.decode("ascii")   # raises if any byte > 0x7F escaped the envelope
        return json.loads(raw)

    def test_bmp_unicode_echoed_in_error(self):
        raw = raw_stdout("net", "ping", "--host", "café.☕")  # café.☕
        env = self._assert_ascii_and_parse(raw)
        self.assertFalse(env["ok"])
        self.assertIn("café.☕", env["error"])   # round-tripped intact

    def test_astral_unicode_survives_ensure_ascii(self):
        # pile-of-poo U+1F4A9 is a surrogate pair when \u-escaped
        raw = raw_stdout("net", "ping", "--host", "x\U0001F4A9y")
        env = self._assert_ascii_and_parse(raw)
        self.assertIn("\U0001F4A9", env["error"])

    def test_unicode_pattern_echoed_by_files(self):
        with tempfile.TemporaryDirectory(prefix="hubfuzz_", dir=str(Path.home())) as d:
            env_vars = {**os.environ, "HUB_FILES_ROOTS": d}
            raw = raw_stdout("files", "search", "--pattern", "résumé*",
                             env=env_vars)
        env = self._assert_ascii_and_parse(raw)
        self.assertTrue(env["ok"])
        self.assertEqual(env["data"]["pattern"], "résumé*")


class TestNotifySanitize(unittest.TestCase):
    def test_control_chars_stripped_newline_kept(self):
        clean, trunc = notify._sanitize("a\x00b\x07c\x1bd", 100)
        self.assertEqual(clean, "abcd")
        self.assertFalse(trunc)
        clean, _ = notify._sanitize("line1\nline2", 100)
        self.assertEqual(clean, "line1\nline2")   # newline preserved

    def test_unicode_preserved(self):
        clean, trunc = notify._sanitize("café ☕", 100)
        self.assertEqual(clean, "café ☕")
        self.assertFalse(trunc)

    def test_clamped_with_truncated_flag(self):
        clean, trunc = notify._sanitize("x" * 100, 10)
        self.assertEqual(clean, "x" * 10)
        self.assertTrue(trunc)

    def test_control_only_becomes_empty(self):
        clean, trunc = notify._sanitize("\x00\x07\x1b", 100)
        self.assertEqual(clean, "")


class TestMailAddrValidation(unittest.TestCase):
    def test_accepts_plain_address(self):
        self.assertTrue(mail._valid_addr("good@example.com"))
        self.assertTrue(mail._valid_addr("  good@example.com  "))  # trimmed

    def test_rejects_header_injection(self):
        for bad in ("good@example.com\nBcc: evil@x.com",
                    "good@example.com\r\nTo: evil@x.com",
                    "good@example.com,evil@x.com",
                    "good@example.com;evil@x.com",
                    "a<b>@example.com"):
            with self.subTest(bad=bad):
                self.assertFalse(mail._valid_addr(bad))

    def test_rejects_malformed(self):
        for bad in ("no-at-sign", "a@b", "@example.com", "a@@b.com", ""):
            with self.subTest(bad=bad):
                self.assertFalse(mail._valid_addr(bad))


if __name__ == "__main__":
    unittest.main(verbosity=2)
