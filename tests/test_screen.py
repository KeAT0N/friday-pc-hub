"""Unit + CLI tests for hub.screen.

The hand-rolled PNG encoder is parsed back and verified chunk-by-chunk. The
capture pipeline (output dir, unique naming, containment, staging) is tested
with the GDI grab MOCKED, so unit tests never write a real screenshot to disk.
`displays` is read-only and gets a live envelope test.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from hub import screen
from tests._helpers import assert_envelope, run_cli

PNG_SIG = b"\x89PNG\r\n\x1a\n"


class _Emitted(Exception):
    pass


def capture_emit(store):
    def fake(ok, action, data=None, error=None):
        store.append({"ok": ok, "action": action, "data": data, "error": error})
        raise _Emitted
    return fake


def parse_chunks(png):
    assert png[:8] == PNG_SIG
    i, chunks = 8, []
    while i < len(png):
        (ln,) = struct.unpack(">I", png[i:i + 4])
        tag = png[i + 4:i + 8]
        data = png[i + 8:i + 8 + ln]
        crc = struct.unpack(">I", png[i + 8 + ln:i + 12 + ln])[0]
        chunks.append((tag, data, crc))
        i += 12 + ln
    return chunks


class TestPngEncoder(unittest.TestCase):
    def test_structure_and_roundtrip(self):
        # 2x1: red, green (already opaque RGBA)
        rgba = bytes([255, 0, 0, 255, 0, 255, 0, 255])
        png = screen._encode_png(2, 1, rgba)
        chunks = parse_chunks(png)
        tags = [c[0] for c in chunks]
        self.assertEqual(tags, [b"IHDR", b"IDAT", b"IEND"])

        w, h, depth, ctype, _, _, _ = struct.unpack(">IIBBBBB", chunks[0][1])
        self.assertEqual((w, h, depth, ctype), (2, 1, 8, 6))  # 8-bit RGBA

        raw = zlib.decompress(chunks[1][1])
        self.assertEqual(raw, b"\x00" + rgba)  # one scanline, filter byte 0

        # every chunk's CRC is valid
        for tag, data, crc in chunks:
            self.assertEqual(crc, zlib.crc32(tag + data) & 0xFFFFFFFF)


class TestChannelSwap(unittest.TestCase):
    def test_bgrx_to_rgba(self):
        bits = bytes([10, 20, 30, 0, 40, 50, 60, 7])  # (B,G,R,X) x2
        out = screen._bgrx_to_rgba(bits)
        self.assertEqual(list(out[:4]), [30, 20, 10, 255])  # R,G,B,A
        self.assertEqual(list(out[4:]), [60, 50, 40, 255])


class TestMonitors(unittest.TestCase):
    def test_live_enumeration(self):
        mons = screen._monitors()
        self.assertGreaterEqual(len(mons), 1)
        self.assertEqual(sum(1 for m in mons if m["primary"]), 1)
        for m in mons:
            self.assertEqual(set(m), {"index", "device", "rect", "work", "primary"})

    def test_virtual_screen_positive(self):
        _, _, w, h = screen._virtual_screen()
        self.assertGreater(w, 0)
        self.assertGreater(h, 0)


class TestCapturePipeline(unittest.TestCase):
    def setUp(self):
        # Sandbox under home (files.allowed_roots refuses AppData/temp).
        self._tmp = tempfile.mkdtemp(prefix="hubshot_", dir=str(Path.home()))
        self._prev = os.environ.get("HUB_FILES_ROOTS")
        os.environ["HUB_FILES_ROOTS"] = self._tmp

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("HUB_FILES_ROOTS", None)
        else:
            os.environ["HUB_FILES_ROOTS"] = self._prev
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def _capture(self, display=None):
        store = []
        rgba = bytes([1, 2, 3, 255] * 6)  # 3x2 synthetic image
        with mock.patch.object(screen, "emit", capture_emit(store)), \
                mock.patch.object(screen, "_grab", return_value=(3, 2, rgba)):
            ns = argparse.Namespace(action="capture", display=display)
            try:
                screen.do_capture(ns)
            except _Emitted:
                pass
        return store[0]

    def test_writes_valid_png_into_allowed_root(self):
        out = self._capture()
        self.assertTrue(out["ok"])
        p = Path(out["data"]["path"])
        self.assertTrue(p.exists())
        self.assertTrue(str(p).startswith(self._tmp))         # contained
        raw = p.read_bytes()
        self.assertEqual(raw[:8], PNG_SIG)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), out["data"]["sha256"])
        self.assertEqual((out["data"]["width"], out["data"]["height"]), (3, 2))

    def test_unique_names_never_overwrite(self):
        first = Path(self._capture()["data"]["path"])
        second = Path(self._capture()["data"]["path"])
        self.assertNotEqual(first, second)
        self.assertTrue(first.exists() and second.exists())

    def test_display_out_of_range_refused(self):
        with mock.patch.object(screen, "_monitors",
                               return_value=[{"rect": [0, 0, 100, 100]}]):
            out = self._capture(display=5)
        self.assertFalse(out["ok"])
        self.assertIn("out of range", out["error"])


class TestScreenCLI(unittest.TestCase):
    def test_displays_envelope(self):
        env, code = run_cli("screen", "displays")
        assert_envelope(env, code)
        self.assertTrue(env["ok"])
        self.assertGreaterEqual(len(env["data"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
