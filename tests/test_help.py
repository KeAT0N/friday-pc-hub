"""Test for hub.help — the human-facing cheat sheet.

It prints plain text (not an envelope), so it's excluded from the envelope
contract. This just guards that the repo-path placeholder is always resolved
and the key commands + Claude-launch hint are present.
"""

from __future__ import annotations

import contextlib
import io
import unittest

from hub import help as hubhelp


class TestHelp(unittest.TestCase):
    def _render(self) -> str:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            hubhelp.main()
        return buf.getvalue()

    def test_repo_placeholder_resolved(self):
        out = self._render()
        self.assertNotIn("__REPO__", out)          # placeholder substituted
        self.assertIn("Desktop\\Claude", out)      # real repo path shown

    def test_lists_key_commands_and_claude_launch(self):
        out = self._render()
        for needle in ("hub friday status", "hub security audit",
                       "hub power lock --confirm", "hub push test",
                       "hub safety kill", "claude"):
            self.assertIn(needle, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
