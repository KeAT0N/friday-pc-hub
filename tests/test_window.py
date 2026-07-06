"""Unit + CLI tests for hub.window.

The snap layout math is a pure function, tested directly. Every mutating op is
tested with the Win32 helpers MOCKED, so no test moves the user's real windows.
`list` is read-only and gets a live envelope test.
"""

from __future__ import annotations

import argparse
import unittest
from unittest import mock

import win32con

from hub import window as win
from hub.apps import WindowInfo
from tests._helpers import assert_envelope, run_cli

WORK = (0, 0, 1920, 1040)   # a taskbar-excluded work area for snap math


class _Emitted(Exception):
    pass


def capture_emit(store):
    def fake(ok, action, data=None, error=None):
        store.append({"ok": ok, "action": action, "data": data, "error": error})
        raise _Emitted
    return fake


class TestSnapGeometry(unittest.TestCase):
    def test_halves_and_quadrants(self):
        self.assertEqual(win._snap_geometry(WORK, "left"), (0, 0, 960, 1040))
        self.assertEqual(win._snap_geometry(WORK, "right"), (960, 0, 960, 1040))
        self.assertEqual(win._snap_geometry(WORK, "top"), (0, 0, 1920, 520))
        self.assertEqual(win._snap_geometry(WORK, "bottom"), (0, 520, 1920, 520))
        self.assertEqual(win._snap_geometry(WORK, "top-right"), (960, 0, 960, 520))
        self.assertEqual(win._snap_geometry(WORK, "bottom-left"),
                         (0, 520, 960, 520))
        self.assertEqual(win._snap_geometry(WORK, "full"), (0, 0, 1920, 1040))

    def test_offset_work_area(self):
        # taskbar on the left: origin shifted, widths adjust
        self.assertEqual(win._snap_geometry((100, 50, 1920, 1080), "left"),
                         (100, 50, 910, 1030))

    def test_unknown_preset_none(self):
        self.assertIsNone(win._snap_geometry(WORK, "nope"))


class SnapHarness(unittest.TestCase):
    """Patches resolve + all Win32 helpers; captures the emitted result."""

    def _snap(self, preset, maximized=False, rect=(0, 0, 800, 600)):
        store = []
        with mock.patch.object(win, "emit", capture_emit(store)), \
                mock.patch.object(win, "resolve", return_value=4242), \
                mock.patch.object(win, "_is_maximized", return_value=maximized), \
                mock.patch.object(win, "_work_area", return_value=WORK), \
                mock.patch.object(win, "_get_rect", return_value=rect), \
                mock.patch.object(win, "_set_pos") as set_pos, \
                mock.patch.object(win, "_show") as show:
            ns = argparse.Namespace(action="snap", to=preset,
                                    hwnd=4242, title=None, pid=None)
            try:
                win.do_snap(ns)
            except _Emitted:
                pass
        return store[0], set_pos, show


class TestSnapDispatch(SnapHarness):
    def test_positional_sets_position(self):
        out, set_pos, show = self._snap("left")
        set_pos.assert_called_once_with(4242, 0, 0, 960, 1040)
        self.assertEqual(out["data"]["to"], "left")

    def test_maximize_uses_showwindow_only(self):
        out, set_pos, show = self._snap("maximize")
        show.assert_called_once_with(4242, win32con.SW_MAXIMIZE)
        set_pos.assert_not_called()

    def test_restores_before_positioning_when_maximized(self):
        out, set_pos, show = self._snap("right", maximized=True)
        show.assert_called_once_with(4242, win32con.SW_RESTORE)
        set_pos.assert_called_once_with(4242, 960, 0, 960, 1040)

    def test_center_uses_current_size(self):
        out, set_pos, show = self._snap("center", rect=(0, 0, 800, 600))
        # centered in 1920x1040: x=(1920-800)//2=560, y=(1040-600)//2=220
        set_pos.assert_called_once_with(4242, 560, 220, 800, 600)


class TestMoveResize(unittest.TestCase):
    def _run(self, handler, ns):
        store = []
        with mock.patch.object(win, "emit", capture_emit(store)), \
                mock.patch.object(win, "resolve", return_value=1), \
                mock.patch.object(win, "_is_maximized", return_value=False), \
                mock.patch.object(win, "_get_rect", return_value=(10, 20, 110, 220)), \
                mock.patch.object(win, "_set_pos") as set_pos:
            try:
                handler(ns)
            except _Emitted:
                pass
        return store[0], set_pos

    def test_move_keeps_size_when_unspecified(self):
        ns = argparse.Namespace(action="move", hwnd=1, title=None, pid=None,
                                x=300, y=400, width=None, height=None)
        out, set_pos = self._run(win.do_move, ns)
        # current size 100x200 preserved
        set_pos.assert_called_once_with(1, 300, 400, 100, 200)

    def test_resize_keeps_position(self):
        ns = argparse.Namespace(action="resize", hwnd=1, title=None, pid=None,
                                width=640, height=480)
        out, set_pos = self._run(win.do_resize, ns)
        set_pos.assert_called_once_with(1, 10, 20, 640, 480)


class TestResolve(unittest.TestCase):
    def _resolve(self, matches):
        store = []
        ns = argparse.Namespace(action="snap", hwnd=None, title="x", pid=None)
        with mock.patch.object(win, "emit", capture_emit(store)), \
                mock.patch.object(win, "enum_windows", return_value=matches):
            try:
                return win.resolve(ns), store
            except _Emitted:
                return None, store

    def test_single_match(self):
        w = WindowInfo(77, 10, "app.exe", "x win", False, False)
        hwnd, store = self._resolve([w])
        self.assertEqual(hwnd, 77)
        self.assertEqual(store, [])

    def test_no_match_errors(self):
        hwnd, store = self._resolve([])
        self.assertIsNone(hwnd)
        self.assertIn("no window matched", store[0]["error"])

    def test_ambiguous_errors_with_candidates(self):
        a = WindowInfo(1, 1, "a.exe", "x", False, False)
        b = WindowInfo(2, 2, "b.exe", "x", False, False)
        hwnd, store = self._resolve([a, b])
        self.assertIsNone(hwnd)
        self.assertIn("disambiguate", store[0]["error"])
        self.assertEqual(len(store[0]["data"]), 2)


class TestWindowCLI(unittest.TestCase):
    def test_list_envelope(self):
        env, code = run_cli("window", "list")
        assert_envelope(env, code)
        self.assertTrue(env["ok"])
        if env["data"]:
            self.assertIn("rect", env["data"][0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
