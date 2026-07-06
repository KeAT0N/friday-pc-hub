"""Unit + CLI tests for hub.media.

Every acting verb is tested with _tap (the keybd_event wrapper) MOCKED, so no
test ever changes the real volume or pauses the user's playback. The read-only
`keys` verb gets a live envelope test.
"""

from __future__ import annotations

import argparse
import unittest
from unittest import mock

from hub import media
from tests._helpers import assert_envelope, run_cli


class _Emitted(Exception):
    pass


def capture_emit(store):
    def fake(ok, action, data=None, error=None):
        store.append({"ok": ok, "action": action, "data": data, "error": error})
        raise _Emitted
    return fake


def send(action, steps=None):
    ns = argparse.Namespace(action=action)
    if steps is not None:
        ns.steps = steps
    return ns


class TestSend(unittest.TestCase):
    def _run(self, ns):
        store = []
        with mock.patch.object(media, "emit", capture_emit(store)), \
                mock.patch.object(media, "_tap") as tap:
            try:
                media.do_send(ns)
            except _Emitted:
                pass
        return store[0], tap

    def test_each_verb_taps_correct_vk(self):
        for verb, vk in media.VK.items():
            with self.subTest(verb=verb):
                out, tap = self._run(send(verb))
                tap.assert_called_once_with(vk)
                self.assertEqual(out["data"]["sent"], verb)

    def test_volume_steps_repeat(self):
        out, tap = self._run(send("volume-up", steps=3))
        self.assertEqual(tap.call_count, 3)
        self.assertEqual(out["data"]["repeats"], 3)

    def test_steps_capped(self):
        out, tap = self._run(send("volume-down", steps=9999))
        self.assertEqual(tap.call_count, media.STEP_CAP)

    def test_steps_floored_to_one(self):
        _, tap = self._run(send("volume-up", steps=0))
        self.assertEqual(tap.call_count, 1)

    def test_non_stepped_verb_taps_once(self):
        # mute has no --steps; even if one leaks in, it stays a single tap.
        _, tap = self._run(send("mute", steps=5))
        self.assertEqual(tap.call_count, 1)


class TestKeys(unittest.TestCase):
    def test_keys_lists_all(self):
        store = []
        with mock.patch.object(media, "emit", capture_emit(store)):
            try:
                media.do_keys()
            except _Emitted:
                pass
        verbs = {k["verb"] for k in store[0]["data"]["keys"]}
        self.assertEqual(verbs, set(media.VK))


class TestMediaCLI(unittest.TestCase):
    def test_keys_envelope(self):
        env, code = run_cli("media", "keys")
        assert_envelope(env, code)
        self.assertTrue(env["ok"])
        self.assertEqual(len(env["data"]["keys"]), len(media.VK))


if __name__ == "__main__":
    unittest.main(verbosity=2)
