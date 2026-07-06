"""Unit + CLI tests for hub.credentials — the secret boundary.

The inline `selftest` proves the Secret cannot leak at a high level; these
tests pin each sealed channel individually and cover the provider registry,
format validation, and the fail-closed enrollment gate.

The real Windows vault is never written: enrollment paths are refused at the
non-TTY / read-only checks that fire BEFORE any `set()`.
"""

from __future__ import annotations

import json
import pickle
import unittest

from hub import credentials as cred
from hub.credentials import Secret
from tests._helpers import assert_envelope, run_cli

CANARY = "canary-3f9a1c-value"


class TestSecretSealing(unittest.TestCase):
    def setUp(self):
        self.s = Secret(CANARY)

    def test_reveal_is_the_only_disclosure(self):
        self.assertEqual(self.s.reveal(), CANARY)

    def test_repr_str_format_redacted(self):
        self.assertNotIn(CANARY, repr(self.s))
        self.assertNotIn(CANARY, str(self.s))
        self.assertNotIn(CANARY, f"{self.s}")
        self.assertNotIn(CANARY, f"{self.s:>20}")  # format spec ignored
        self.assertNotIn(CANARY, "value=%s" % self.s)
        self.assertEqual(repr(self.s), cred.REDACTED)

    def test_json_dump_raises(self):
        with self.assertRaises(TypeError):
            json.dumps({"s": self.s})

    def test_pickle_raises(self):
        with self.assertRaises(TypeError):
            pickle.dumps(self.s)

    def test_len_raises(self):
        with self.assertRaises(TypeError):
            len(self.s)

    def test_immutable_setattr_delattr(self):
        with self.assertRaises(AttributeError):
            self.s._value = "x"
        with self.assertRaises(AttributeError):
            del self.s._value

    def test_unhashable(self):
        self.assertIsNone(Secret.__hash__)
        with self.assertRaises(TypeError):
            hash(self.s)
        with self.assertRaises(TypeError):
            {self.s}

    def test_bool_is_true(self):
        self.assertTrue(bool(self.s))

    def test_constant_time_equality(self):
        self.assertEqual(Secret(CANARY), Secret(CANARY))
        self.assertNotEqual(Secret(CANARY), Secret("different"))

    def test_eq_with_bare_str_is_false(self):
        # __eq__ returns NotImplemented for non-Secret -> Python falls back to
        # identity, so a Secret never compares equal to a raw string.
        self.assertFalse(self.s == CANARY)

    def test_empty_or_nonstr_rejected(self):
        with self.assertRaises(TypeError):
            Secret("")
        with self.assertRaises(TypeError):
            Secret(b"bytes")  # type: ignore[arg-type]


class TestProviderRegistry(unittest.TestCase):
    def test_default_resolves_to_null(self):
        p = cred.get_provider()
        self.assertEqual(p.name, "null")

    def test_null_refuses_and_reports_absent(self):
        p = cred.get_provider("null")
        self.assertFalse(p.exists("svc", "acct"))
        with self.assertRaises(cred.CredentialNotFound):
            p.get("svc", "acct")

    def test_null_is_read_only(self):
        p = cred.get_provider("null")
        with self.assertRaises(cred.ProviderUnavailable):
            p.set("svc", "acct", Secret("x"))

    def test_unknown_provider_raises(self):
        with self.assertRaises(cred.ProviderUnavailable):
            cred.get_provider("nope-not-real")

    def test_env_provider_gated_but_maps_var_names(self):
        self.assertFalse(cred.EnvProvider.available())
        self.assertEqual(
            cred.EnvProvider.var_name("icloud mail", "keaton"),
            "HUB_CRED_ICLOUD_MAIL__KEATON")


class TestFormatValidation(unittest.TestCase):
    def test_app_password_shape(self):
        self.assertIsNone(cred._format_error(
            Secret("abcd-efgh-ijkl-mnop"), "app-password"))

    def test_app_password_mismatch(self):
        err = cred._format_error(Secret("NOT-a-valid-shape"), "app-password")
        self.assertIsNotNone(err)
        self.assertIn("app-password", err)

    def test_custom_regex_fullmatch(self):
        self.assertIsNone(cred._format_error(Secret("123456"), r"\d{6}"))
        self.assertIsNotNone(cred._format_error(Secret("12345"), r"\d{6}"))

    def test_invalid_regex_reported_not_raised(self):
        err = cred._format_error(Secret("x"), "[unterminated")
        self.assertIn("invalid", err.lower())

    def test_format_error_never_contains_secret(self):
        err = cred._format_error(Secret(CANARY), "app-password")
        self.assertNotIn(CANARY, err)


class TestCLIExistenceOnly(unittest.TestCase):
    def test_providers_lists_backends(self):
        env, code = run_cli("credentials", "providers")
        assert_envelope(env, code)
        by_name = {p["name"]: p for p in env["data"]}
        self.assertTrue(by_name["null"]["available"])
        self.assertFalse(by_name["null"]["writable"])
        self.assertFalse(by_name["env"]["available"])
        self.assertTrue(by_name["keyring"]["writable"])

    def test_selftest_passes(self):
        env, code = run_cli("credentials", "selftest")
        assert_envelope(env, code)
        self.assertTrue(env["ok"])

    def test_check_returns_bool_no_value_field(self):
        env, code = run_cli("credentials", "check",
                            "--service", "no-such-service-xyz")
        assert_envelope(env, code)
        self.assertIn("exists", env["data"])
        self.assertIsInstance(env["data"]["exists"], bool)
        # existence-only contract: nothing that could be a secret value.
        self.assertNotIn("value", env["data"])
        self.assertNotIn("secret", env["data"])

    def test_enroll_non_tty_refused_before_vault_write(self):
        # stdin is DEVNULL (not a TTY); enroll must refuse and never prompt or
        # write. Default provider is keyring, but the TTY gate fires first.
        env, code = run_cli("credentials", "enroll", "--service", "unittest-x")
        assert_envelope(env, code)
        self.assertFalse(env["ok"])
        self.assertIn("terminal", env["error"].lower())

    def test_enroll_stdin_to_readonly_provider_refused(self):
        # null is read-only; the writable-provider check fires before the
        # secret is even read from stdin.
        env, code = run_cli("credentials", "enroll", "--service", "unittest-x",
                            "--provider", "null", "--stdin", input="whatever\n")
        assert_envelope(env, code)
        self.assertFalse(env["ok"])
        self.assertIn("read-only", env["error"].lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
