"""Bounded-loop regression guard (see docs/AUDIT.md).

Asserts the constants that bound the hub's loops/waits still exist and are
sane. If a future change removes or zeroes a cap, this fails — keeping the
no-hang guarantee from silently eroding. Also directly checks the two loops
tightened by the audit (system --sample clamp, screen unique-name ceiling).
"""

from __future__ import annotations

import unittest

from hub import clipboard, files, media, net, safety, screen, security, system
from hub import friday, power


class TestBoundingConstants(unittest.TestCase):
    def test_safety_poll_interval_positive(self):
        self.assertGreater(safety.POLL_INTERVAL, 0)
        self.assertLessEqual(safety.POLL_INTERVAL, 1.0)

    def test_files_search_ceilings(self):
        self.assertGreaterEqual(files.SEARCH_SCAN_CAP, 1000)
        self.assertGreaterEqual(files.SEARCH_RESULT_CAP, 1)
        self.assertGreaterEqual(files.KILL_CHECK_EVERY, 1)
        self.assertLessEqual(files.SEARCH_TIMEOUT_CAP, 600)
        self.assertGreater(files.HASH_DEADLINE_SEC, 0)
        self.assertGreater(files.READ_HARD_CAP, 0)

    def test_clipboard_bounds(self):
        self.assertGreater(clipboard.OPEN_DEADLINE_SEC, 0)
        self.assertGreater(clipboard.READ_HARD_CAP, 0)

    def test_net_ping_cap(self):
        self.assertGreaterEqual(net.PING_COUNT_CAP, 1)
        self.assertLessEqual(net.PING_COUNT_CAP, 100)
        self.assertGreater(net.SUBPROC_TIMEOUT, 0)

    def test_security_intruders_caps(self):
        self.assertGreaterEqual(security.INTRUDERS_HOURS_CAP, 1)
        self.assertGreaterEqual(security.INTRUDERS_MAX_CAP, 1)
        self.assertGreater(security.PS_TIMEOUT, 0)
        self.assertGreater(security.HARDEN_TIMEOUT, 0)

    def test_media_step_cap(self):
        self.assertGreaterEqual(media.STEP_CAP, 1)
        self.assertLessEqual(media.STEP_CAP, 1000)

    def test_system_sample_cap(self):
        self.assertGreater(system.SAMPLE_MAX, 0)
        self.assertLessEqual(system.SAMPLE_MAX, 60)

    def test_screen_unique_cap(self):
        self.assertGreaterEqual(screen.UNIQUE_CAP, 1)

    def test_friday_module_timeout(self):
        self.assertGreater(friday.MODULE_TIMEOUT, 0)
        self.assertGreater(friday.SCHTASKS_TIMEOUT, 0)

    def test_power_timeouts(self):
        self.assertGreater(power.SUBPROC_TIMEOUT, 0)
        self.assertGreater(power.MONITOR_MSG_TIMEOUT, 0)


class TestClampBehavior(unittest.TestCase):
    def test_system_sample_is_clamped(self):
        self.assertEqual(system._clamp_sample(99999), system.SAMPLE_MAX)
        self.assertEqual(system._clamp_sample(-5), 0.0)
        self.assertEqual(system._clamp_sample(0.05), 0.05)

    def test_unique_path_terminates_when_all_exist(self):
        # simulate a directory where every candidate 'exists' — must still
        # return promptly (bounded), never spin forever.
        class AlwaysExists:
            def __truediv__(self, other):
                return self
            def exists(self):
                return True
            def __fspath__(self):
                return "x"
        # patch Path.exists via a fake directory object
        result = screen._unique_path(AlwaysExists())
        self.assertIsNotNone(result)   # returned, did not hang


if __name__ == "__main__":
    unittest.main(verbosity=2)
