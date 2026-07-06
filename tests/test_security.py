"""Unit + CLI tests for hub.security.

The PowerShell gather is integration-tested via one live `audit` smoke. The
deterministic logic — concern flagging and notes/truncation — is unit-tested on
captured sample dicts so it never depends on the live machine's posture.
"""

from __future__ import annotations

import unittest

from hub import security
from tests._helpers import assert_envelope, run_cli

CLEAN = {
    "elevated": True,
    "defender": {"realtime": True, "antivirus": True, "tamper": True},
    "firewall": {"Domain": True, "Private": True, "Public": True},
    "uac_enabled": True, "rdp_enabled": False, "smb1_enabled": False,
    "guest_enabled": False,
}

BAD = {
    "elevated": True,
    "defender": {"realtime": False, "antivirus": False, "tamper": False},
    "firewall": {"Domain": True, "Private": True, "Public": False},
    "uac_enabled": False, "rdp_enabled": True, "smb1_enabled": True,
    "guest_enabled": True,
}


class TestConcerns(unittest.TestCase):
    def test_clean_posture_has_no_concerns(self):
        self.assertEqual(security.concerns(CLEAN), [])

    def test_bad_posture_flags_every_invariant(self):
        c = security.concerns(BAD)
        self.assertIn("Defender real-time protection is OFF", c)
        self.assertIn("Defender antivirus is OFF", c)
        self.assertTrue(any("Public" in x for x in c))
        self.assertIn("UAC is disabled", c)
        self.assertTrue(any("RDP" in x for x in c))
        self.assertTrue(any("SMBv1" in x for x in c))
        self.assertTrue(any("Guest" in x for x in c))

    def test_null_defender_does_not_crash(self):
        self.assertEqual(security.concerns({"defender": None}), [])

    def test_no_numeric_threshold_flags(self):
        # signature age is reported, never flagged (no tunable thresholds here)
        a = {"defender": {"realtime": True, "signature_age_days": 99}}
        self.assertEqual(security.concerns(a), [])


class TestNotes(unittest.TestCase):
    def test_unelevated_null_checks_noted(self):
        a = {"elevated": False, "bitlocker": None, "local_admins": None}
        notes = security._notes(a)
        self.assertTrue(any("not elevated" in n for n in notes))
        self.assertTrue(any("bitlocker" in n for n in notes))

    def test_elevated_no_note(self):
        self.assertEqual(security._notes({"elevated": True}), [])

    def test_listening_ports_truncated(self):
        a = {"elevated": True,
             "listening": {"count": 100,
                           "ports": [{"port": i} for i in range(100)]}}
        notes = security._notes(a)
        self.assertEqual(len(a["listening"]["ports"]), security.LISTEN_SAMPLE)
        self.assertTrue(any("truncated" in n for n in notes))


class TestIntruderConcerns(unittest.TestCase):
    def test_detections_flagged(self):
        a = {"defender_detections": {"available": True, "count": 2}}
        c = security.intruder_concerns(a)
        self.assertTrue(any("2 recent threat" in x for x in c))

    def test_zero_detections_no_concern(self):
        a = {"defender_detections": {"available": True, "count": 0}}
        self.assertEqual(security.intruder_concerns(a), [])

    def test_unavailable_detections_no_crash(self):
        a = {"defender_detections": {"available": False, "error": "x"}}
        self.assertEqual(security.intruder_concerns(a), [])

    def test_missing_key_no_crash(self):
        self.assertEqual(security.intruder_concerns({}), [])


class TestIntruderNotes(unittest.TestCase):
    def test_unavailable_failed_logons_noted(self):
        a = {"failed_logons": {"available": False, "error": "access denied"}}
        notes = security._intruder_notes(a)
        self.assertTrue(any("elevated terminal" in n for n in notes))

    def test_available_failed_logons_no_note(self):
        a = {"failed_logons": {"available": True, "count": 0}}
        self.assertEqual(security._intruder_notes(a), [])


class TestSecurityCLI(unittest.TestCase):
    def test_audit_live_envelope(self):
        env, code = run_cli("security", "audit", timeout=50)
        assert_envelope(env, code)
        self.assertTrue(env["ok"])
        for key in ("defender", "firewall", "concerns", "notes"):
            self.assertIn(key, env["data"])
        self.assertIsInstance(env["data"]["concerns"], list)

    def test_intruders_live_envelope(self):
        env, code = run_cli("security", "intruders", "--hours", "1", "--max", "5",
                            timeout=50)
        assert_envelope(env, code)
        self.assertTrue(env["ok"])
        for key in ("failed_logons", "defender_detections", "concerns", "notes"):
            self.assertIn(key, env["data"])

    def test_intruders_hours_capped(self):
        env, code = run_cli("security", "intruders", "--hours", "9999",
                            "--max", "5", timeout=50)
        assert_envelope(env, code)
        fl = env["data"]["failed_logons"]
        if fl.get("available") and "window_hours" in fl:
            self.assertLessEqual(fl["window_hours"], security.INTRUDERS_HOURS_CAP)


if __name__ == "__main__":
    unittest.main(verbosity=2)
