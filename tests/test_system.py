"""Unit + CLI tests for hub.system — read-only telemetry.

Section functions return plain dicts, so most are tested in-process. A short
CPU sample keeps the suite fast. The load-bearing invariants: percent fields
are whole-machine 0-100, per-core count matches the logical CPU count, top
processes are normalized (not per-core) and exclude the System Idle Process.
"""

from __future__ import annotations

import unittest

from hub import system as sysmod
from tests._helpers import assert_envelope, run_cli

FAST = 0.05  # CPU sample window for tests


class TestCPU(unittest.TestCase):
    def test_shape_and_normalization(self):
        d = sysmod.sec_cpu(FAST)
        self.assertEqual(
            set(d), {"percent", "per_core_percent", "cores_logical",
                     "cores_physical", "freq_mhz", "sample_sec"})
        self.assertGreaterEqual(d["percent"], 0.0)
        self.assertLessEqual(d["percent"], 100.0)
        self.assertEqual(len(d["per_core_percent"]), d["cores_logical"])
        self.assertEqual(d["sample_sec"], FAST)
        # overall percent is the mean of the per-core samples (whole-machine)
        expected = round(sum(d["per_core_percent"]) / len(d["per_core_percent"]), 1)
        self.assertAlmostEqual(d["percent"], expected, places=1)


class TestMemory(unittest.TestCase):
    def test_invariants(self):
        d = sysmod.sec_memory()
        self.assertGreater(d["total_gb"], 0)
        self.assertLessEqual(d["used_gb"], d["total_gb"])
        self.assertLessEqual(d["available_gb"], d["total_gb"])
        self.assertGreaterEqual(d["percent"], 0.0)
        self.assertLessEqual(d["percent"], 100.0)
        self.assertIn("swap_total_gb", d)
        self.assertIn("swap_percent", d)


class TestDisk(unittest.TestCase):
    def test_partitions_have_usage(self):
        rows = sysmod.sec_disk()
        self.assertGreaterEqual(len(rows), 1)  # at least the system drive
        for r in rows:
            self.assertEqual(
                set(r), {"mount", "fstype", "total_gb", "free_gb", "percent"})
            self.assertLessEqual(r["free_gb"], r["total_gb"])
            self.assertGreaterEqual(r["percent"], 0.0)
            self.assertLessEqual(r["percent"], 100.0)


class TestNetwork(unittest.TestCase):
    def test_totals_and_nics(self):
        d = sysmod.sec_network()
        self.assertGreaterEqual(d["total"]["sent_mb"], 0)
        self.assertGreaterEqual(d["total"]["recv_mb"], 0)
        self.assertIsInstance(d["nics"], dict)
        for nic in d["nics"].values():
            self.assertEqual(
                set(nic), {"sent_mb", "recv_mb", "errors", "drops"})


class TestBattery(unittest.TestCase):
    def test_structure_present_or_absent(self):
        d = sysmod.sec_battery()
        self.assertIn("present", d)
        if d["present"]:
            self.assertGreaterEqual(d["percent"], 0.0)
            self.assertLessEqual(d["percent"], 100.0)
            self.assertIsInstance(d["plugged_in"], bool)
        else:
            self.assertEqual(d, {"present": False})


class TestUptime(unittest.TestCase):
    def test_positive(self):
        d = sysmod.sec_uptime()
        self.assertGreater(d["boot_epoch"], 0)
        self.assertGreater(d["uptime_sec"], 0)


class TestTop(unittest.TestCase):
    def test_excludes_idle_and_is_normalized(self):
        rows = sysmod.sec_top("cpu", 5, FAST)
        self.assertLessEqual(len(rows), 5)
        for r in rows:
            self.assertEqual(set(r), {"pid", "name", "cpu_percent", "memory_mb"})
            self.assertNotEqual(r["pid"], 0, "System Idle Process must be excluded")
            self.assertGreaterEqual(r["cpu_percent"], 0.0)
            # normalized to whole-machine: must be far below cores*100, which
            # would be the un-normalized ceiling (e.g. 2400% on 24 cores).
            self.assertLessEqual(r["cpu_percent"], 200.0)

    def test_sorted_descending(self):
        rows = sysmod.sec_top("memory", 5, 0)
        vals = [r["memory_mb"] for r in rows]
        self.assertEqual(vals, sorted(vals, reverse=True))


class TestSnapshotCLI(unittest.TestCase):
    def test_snapshot_envelope_and_sections(self):
        env, code = run_cli("system", "snapshot", "--sample", "0.05", "--top", "3")
        assert_envelope(env, code)
        self.assertTrue(env["ok"])
        self.assertEqual(
            set(env["data"]),
            {"uptime", "cpu", "memory", "disk", "network", "battery",
             "top_cpu", "top_memory"})
        self.assertLessEqual(len(env["data"]["top_cpu"]), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
