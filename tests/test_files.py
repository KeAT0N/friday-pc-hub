"""Unit + CLI tests for hub.files — the containment boundary.

A private sandbox root is installed via HUB_FILES_ROOTS so tests never touch
the user's real Desktop/Documents. Covers: allowlist containment, `..` and
outside escapes, refused dot-dirs, key-material patterns, binary refusal, and
bounded/truncated reads.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from hub import files
from tests._helpers import assert_envelope, run_cli


class FilesSandbox(unittest.TestCase):
    def setUp(self):
        # Site the sandbox under the home dir, NOT the system temp dir: temp
        # lives under ...\AppData\Local\Temp and `appdata` is a refused
        # directory, so a temp-rooted sandbox would be (correctly) blocked.
        self._tmp = tempfile.mkdtemp(prefix="hubfiles_", dir=str(Path.home()))
        self.root = Path(self._tmp).resolve()
        # Populate the sandbox.
        (self.root / "notes.txt").write_text("hello world\n" * 10, encoding="utf-8")
        (self.root / "data.bin").write_bytes(b"PNG\x00\x00\x01binary\x00stuff")
        (self.root / "secret.pem").write_text("-----KEY-----", encoding="utf-8")
        (self.root / "id_rsa").write_text("PRIVATE", encoding="utf-8")
        (self.root / ".ssh").mkdir()
        (self.root / ".ssh" / "id_key").write_text("k", encoding="utf-8")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "deep.txt").write_text("deep", encoding="utf-8")

        self._prev = os.environ.get(files.ENV_ROOTS)
        os.environ[files.ENV_ROOTS] = str(self.root)
        self.cli_env = dict(os.environ)

    def tearDown(self):
        if self._prev is None:
            os.environ.pop(files.ENV_ROOTS, None)
        else:
            os.environ[files.ENV_ROOTS] = self._prev
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)


class TestContainment(FilesSandbox):
    def test_allowed_roots_reads_env_and_filters_nondirs(self):
        bogus = str(self.root / "does-not-exist")
        os.environ[files.ENV_ROOTS] = f"{self.root};{bogus}"
        roots = files.allowed_roots()
        self.assertEqual(roots, [self.root])

    def test_path_inside_root_resolves(self):
        p = files.resolve_contained(str(self.root / "notes.txt"), want_file=True)
        self.assertEqual(p, self.root / "notes.txt")

    def test_outside_territory_refused(self):
        outside = str(Path(tempfile.gettempdir()).resolve() / "elsewhere.txt")
        with self.assertRaises(files.ContainmentError):
            files.resolve_contained(outside)

    def test_dotdot_escape_refused(self):
        # Resolves to the parent of the sandbox — outside territory.
        escape = str(self.root / ".." / "escape.txt")
        with self.assertRaises(files.ContainmentError):
            files.resolve_contained(escape)

    def test_refused_dotdir_blocks_even_inside_territory(self):
        with self.assertRaises(files.ContainmentError):
            files.resolve_contained(str(self.root / ".ssh" / "id_key"))

    def test_key_material_patterns_refused(self):
        for name in ("secret.pem", "id_rsa"):
            with self.assertRaises(files.ContainmentError):
                files.resolve_contained(str(self.root / name), want_file=True)

    def test_nonexistent_inside_territory_refused(self):
        with self.assertRaises(files.ContainmentError):
            files.resolve_contained(str(self.root / "ghost.txt"))

    def test_want_file_rejects_directory(self):
        with self.assertRaises(files.ContainmentError):
            files.resolve_contained(str(self.root / "sub"), want_file=True)


class TestReadCLI(FilesSandbox):
    def test_read_text_within_cap(self):
        env, code = run_cli("files", "read", "--path",
                            str(self.root / "notes.txt"), env=self.cli_env)
        assert_envelope(env, code)
        self.assertTrue(env["ok"])
        self.assertIn("hello world", env["data"]["content"])
        self.assertFalse(env["data"]["truncated"])

    def test_read_truncates_and_flags(self):
        env, code = run_cli("files", "read", "--path",
                            str(self.root / "notes.txt"),
                            "--max-bytes", "10", env=self.cli_env)
        assert_envelope(env, code)
        self.assertTrue(env["data"]["truncated"])
        self.assertEqual(env["data"]["returned_bytes"], 10)
        self.assertEqual(env["data"]["window"], "head")

    def test_read_tail_window(self):
        env, code = run_cli("files", "read", "--path",
                            str(self.root / "notes.txt"),
                            "--max-bytes", "12", "--tail", env=self.cli_env)
        assert_envelope(env, code)
        self.assertEqual(env["data"]["window"], "tail")

    def test_binary_refused(self):
        env, code = run_cli("files", "read", "--path",
                            str(self.root / "data.bin"), env=self.cli_env)
        assert_envelope(env, code)
        self.assertFalse(env["ok"])
        self.assertIn("binary", env["error"].lower())

    def test_key_material_read_refused(self):
        env, code = run_cli("files", "read", "--path",
                            str(self.root / "secret.pem"), env=self.cli_env)
        assert_envelope(env, code)
        self.assertFalse(env["ok"])


class TestSearchCLI(FilesSandbox):
    def test_search_finds_by_glob(self):
        env, code = run_cli("files", "search", "--pattern", "*.txt",
                            env=self.cli_env)
        assert_envelope(env, code)
        names = {Path(m["path"]).name for m in env["data"]["matches"]}
        self.assertIn("notes.txt", names)
        self.assertIn("deep.txt", names)  # recursion into sub/
        self.assertTrue(env["data"]["complete"])

    def test_search_excludes_key_material(self):
        env, code = run_cli("files", "search", "--pattern", "*.pem",
                            env=self.cli_env)
        assert_envelope(env, code)
        self.assertEqual(env["data"]["matches"], [])

    def test_search_prunes_refused_dirs(self):
        # id_key lives under .ssh; a broad search must never surface it.
        env, code = run_cli("files", "search", "--pattern", "id_*",
                            env=self.cli_env)
        assert_envelope(env, code)
        paths = [m["path"] for m in env["data"]["matches"]]
        self.assertFalse(any(".ssh" in p for p in paths))
        self.assertFalse(any(Path(p).name == "id_rsa" for p in paths))


class TestStageCLI(FilesSandbox):
    def test_stage_returns_hash_and_clearance(self):
        env, code = run_cli("files", "stage", "--path",
                            str(self.root / "notes.txt"), env=self.cli_env)
        assert_envelope(env, code)
        self.assertTrue(env["ok"])
        self.assertEqual(len(env["data"]["sha256"]), 64)
        self.assertTrue(env["data"]["cleared_for_send"])

    def test_stage_over_cap_refused(self):
        env, code = run_cli("files", "stage", "--path",
                            str(self.root / "notes.txt"),
                            "--max-mb", "0", env=self.cli_env)
        assert_envelope(env, code)
        self.assertFalse(env["ok"])
        self.assertIn("exceeds", env["error"].lower())

    def test_stage_key_material_refused(self):
        env, code = run_cli("files", "stage", "--path",
                            str(self.root / "id_rsa"), env=self.cli_env)
        assert_envelope(env, code)
        self.assertFalse(env["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
