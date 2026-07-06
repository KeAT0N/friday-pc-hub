"""Unit + CLI tests for hub.net — read-only network telemetry.

Section functions are tested in-process; the ping parser is tested on captured
sample text (locale-independent, and a regression guard for the bytes=/TTL=
misparse). Live tests use only local/loopback paths.
"""

from __future__ import annotations

import argparse
import unittest
from unittest import mock

from hub import net
from tests._helpers import assert_envelope, run_cli

PING_OK = """
Pinging 127.0.0.1 with 32 bytes of data:
Reply from 127.0.0.1: bytes=32 time<1ms TTL=128
Reply from 127.0.0.1: bytes=32 time<1ms TTL=128
Reply from 127.0.0.1: bytes=32 time<1ms TTL=128
Reply from 127.0.0.1: bytes=32 time<1ms TTL=128

Ping statistics for 127.0.0.1:
    Packets: Sent = 4, Received = 4, Lost = 0 (0% loss),
Approximate round trip times in milli-seconds:
    Minimum = 0ms, Maximum = 1ms, Average = 0ms
"""

PING_LOSS = """
Ping statistics for 8.8.8.8:
    Packets: Sent = 4, Received = 1, Lost = 3 (75% loss),
Approximate round trip times in milli-seconds:
    Minimum = 20ms, Maximum = 40ms, Average = 30ms
"""


class TestPingParse(unittest.TestCase):
    def test_counts_not_bytes_or_ttl(self):
        d = net._parse_ping(PING_OK)
        self.assertEqual(d["transmitted"], 4)   # NOT 32 (bytes=) or 128 (TTL=)
        self.assertEqual(d["received"], 4)
        self.assertEqual(d["loss_percent"], 0)
        self.assertEqual(d["avg_ms"], 0)

    def test_partial_loss(self):
        d = net._parse_ping(PING_LOSS)
        self.assertEqual(d["transmitted"], 4)
        self.assertEqual(d["received"], 1)
        self.assertEqual(d["loss_percent"], 75)
        self.assertEqual(d["avg_ms"], 30)

    def test_unparseable_is_none_not_garbage(self):
        d = net._parse_ping("no numbers here")
        self.assertEqual(d, {"transmitted": None, "received": None,
                             "loss_percent": None, "avg_ms": None})


class TestSections(unittest.TestCase):
    def test_status_shape(self):
        d = net.sec_status()
        self.assertIsInstance(d["hostname"], str)
        self.assertTrue(d["hostname"])
        self.assertIsInstance(d["online"], bool)
        self.assertTrue(d["local_ip"] is None or isinstance(d["local_ip"], str))

    def test_adapters_shape(self):
        rows = net.sec_adapters()
        self.assertGreaterEqual(len(rows), 1)  # loopback always present
        for r in rows:
            self.assertEqual(set(r), {"name", "is_up", "speed_mbps",
                                      "ipv4", "ipv6", "mac"})
            self.assertIsInstance(r["ipv4"], list)

    def test_wifi_has_present_flag(self):
        d = net.sec_wifi()
        self.assertIn("present", d)
        self.assertIsInstance(d["present"], bool)


class TestPingHostValidation(unittest.TestCase):
    def _ping(self, host):
        store = []

        def fake(ok, action, data=None, error=None):
            store.append({"ok": ok, "error": error})
            raise RuntimeError("emitted")
        with mock.patch.object(net, "emit", fake):
            try:
                net.do_ping(host, 1)
            except RuntimeError:
                pass
        return store[0]

    def test_rejects_injection_and_bad_chars(self):
        for bad in ("-rf", "bad!host", "a b", "-", ".startdot"):
            with self.subTest(bad=bad):
                out = self._ping(bad)
                self.assertFalse(out["ok"])
                self.assertIn("invalid host", out["error"])


class TestNetCLI(unittest.TestCase):
    def test_adapters_envelope(self):
        env, code = run_cli("net", "adapters")
        assert_envelope(env, code)
        self.assertTrue(env["ok"])

    def test_ping_loopback_reachable(self):
        env, code = run_cli("net", "ping", "--host", "127.0.0.1", "--count", "1")
        assert_envelope(env, code)
        self.assertTrue(env["data"]["reachable"])
        self.assertEqual(env["data"]["loss_percent"], 0)

    def test_ping_count_capped(self):
        env, code = run_cli("net", "ping", "--host", "127.0.0.1", "--count", "999")
        assert_envelope(env, code)
        self.assertLessEqual(env["data"]["count"], net.PING_COUNT_CAP)


if __name__ == "__main__":
    unittest.main(verbosity=2)
