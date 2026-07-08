"""
dashboard/terminal_widget.py — a real embedded terminal for the dashboard.

Spawns a PowerShell child in a Windows pseudo-console (pywinpty), feeds its
byte stream through a pyte VT100 emulator (so cursor moves, colors, and
in-place redraws — e.g. PSReadLine — render correctly instead of scrolling
garbage), and paints the resulting screen buffer with QPainter. Keystrokes
are translated back into the VT sequences the shell expects.

Scrollback: pyte's HistoryScreen archives lines as they scroll off the top;
the mouse wheel (or Shift+PageUp/PageDown) slides the view over
history+live rows. New output keeps the view pinned in place; typing snaps
back to the live bottom.

This widget owns exactly one child process for its lifetime; closing it
terminates the shell.
"""

from __future__ import annotations

from itertools import islice

import pyte
import winpty
from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPalette
from PySide6.QtWidgets import QWidget

SHELL = ["powershell.exe", "-NoLogo"]
DEFAULT_FG = "#d4d4d4"
DEFAULT_BG = "#0c1016"  # matches the dashboard theme's #termFrame background
CURSOR_COLOR = "#9ecbff"
SCROLLBAR_COLOR = "#2f4266"
PAD = 10                # inner padding in px so text isn't glued to the frame edge
HISTORY_LINES = 5000    # scrollback depth
WHEEL_UNIT = 40         # wheel angle units per scrolled line (~3 lines/notch)

ANSI_COLORS = {
    "black": "#000000", "red": "#cd3131", "green": "#0dbc79", "brown": "#e5e510",
    "blue": "#2472c8", "magenta": "#bc3fbc", "cyan": "#11a8cd", "white": "#e5e5e5",
    "brightblack": "#666666", "brightred": "#f14c4c", "brightgreen": "#23d18b",
    "brightbrown": "#f5f543", "brightblue": "#3b8eea", "brightmagenta": "#d670d6",
    "brightcyan": "#29b8db", "brightwhite": "#ffffff",
}

# Qt key -> VT100/xterm escape sequence, for keys with no printable text().
SPECIAL_KEYS = {
    Qt.Key_Up: "\x1b[A", Qt.Key_Down: "\x1b[B",
    Qt.Key_Right: "\x1b[C", Qt.Key_Left: "\x1b[D",
    Qt.Key_Home: "\x1b[H", Qt.Key_End: "\x1b[F",
    Qt.Key_PageUp: "\x1b[5~", Qt.Key_PageDown: "\x1b[6~",
    Qt.Key_Insert: "\x1b[2~", Qt.Key_Delete: "\x1b[3~",
    Qt.Key_Backspace: "\x7f", Qt.Key_Tab: "\t",
    Qt.Key_Return: "\r", Qt.Key_Enter: "\r",
    Qt.Key_Escape: "\x1b",
}


def _resolve_color(value: str, default: str) -> QColor:
    if value == "default":
        return QColor(default)
    if value in ANSI_COLORS:
        return QColor(ANSI_COLORS[value])
    if len(value) == 6:  # pyte truecolor: bare hex, no '#'
        return QColor(f"#{value}")
    return QColor(default)


class _PtyReader(QThread):
    """Blocking PTY reads live on this thread; decoded chunks cross to the
    GUI thread via a queued signal connection (Qt handles the marshalling)."""

    data_received = Signal(str)
    process_exited = Signal()

    def __init__(self, pty: winpty.PtyProcess, parent=None):
        super().__init__(parent)
        self._pty = pty
        self._stop = False

    def run(self) -> None:
        while not self._stop:
            try:
                chunk = self._pty.read(4096)
            except (EOFError, OSError, winpty.WinptyError):
                break
            if not chunk:
                break
            self.data_received.emit(chunk)
        self.process_exited.emit()

    def stop(self) -> None:
        self._stop = True


class TerminalWidget(QWidget):
    """A minimal xterm-alike: pyte does the emulation, this widget just
    paints the resulting cell buffer and forwards keystrokes to the PTY."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent)

        pal = self.palette()
        pal.setColor(QPalette.Window, QColor(DEFAULT_BG))
        self.setPalette(pal)
        self.setAutoFillBackground(True)

        self._font = QFont("Cascadia Mono", 10)
        if not self._font.exactMatch():
            self._font = QFont("Consolas", 10)
        self._font.setStyleHint(QFont.Monospace)
        self.setFont(self._font)
        fm = QFontMetrics(self._font)
        self._char_w = max(1, fm.horizontalAdvance("M"))
        self._char_h = max(1, fm.height())

        self._scroll = 0        # lines scrolled up into history; 0 = live view
        self._wheel_accum = 0

        cols, rows = self._cell_size(self.width() or 800, self.height() or 500)
        self.screen = pyte.HistoryScreen(cols, rows, history=HISTORY_LINES)
        self.stream = pyte.Stream(self.screen)

        self.pty = winpty.PtyProcess.spawn(SHELL, dimensions=(rows, cols))
        self._reader = _PtyReader(self.pty, self)
        self._reader.data_received.connect(self._on_data)
        self._reader.process_exited.connect(self._on_exit)
        self._reader.start()

    # ------------------------------------------------------------ geometry

    def _cell_size(self, width: int, height: int) -> tuple[int, int]:
        cols = max(10, (width - 2 * PAD) // self._char_w)
        rows = max(4, (height - 2 * PAD) // self._char_h)
        return cols, rows

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        cols, rows = self._cell_size(self.width(), self.height())
        if (cols, rows) != (self.screen.columns, self.screen.lines):
            self.screen.resize(rows, cols)
            try:
                self.pty.setwinsize(rows, cols)
            except (OSError, winpty.WinptyError):
                pass  # process may already be gone

    # ------------------------------------------------------------ data flow

    def _on_data(self, chunk: str) -> None:
        before = len(self.screen.history.top)
        self.stream.feed(chunk)
        if self._scroll:
            # keep the view pinned on the same content as history grows
            grown = len(self.screen.history.top) - before
            self._scroll = min(self._scroll + grown,
                                len(self.screen.history.top))
        self.update()

    def _on_exit(self) -> None:
        self.update()

    def is_alive(self) -> bool:
        return self.pty.isalive()

    def shutdown(self) -> None:
        self._reader.stop()
        try:
            if self.pty.isalive():
                self.pty.terminate(force=True)
        except (OSError, winpty.WinptyError):
            pass
        self._reader.wait(2000)

    # ------------------------------------------------------------ scrollback

    def _scroll_by(self, lines: int) -> None:
        top_len = len(self.screen.history.top)
        self._scroll = max(0, min(top_len, self._scroll + lines))
        self.update()

    def wheelEvent(self, event) -> None:
        self._wheel_accum += event.angleDelta().y()
        lines = self._wheel_accum // WHEEL_UNIT
        if lines:
            self._wheel_accum -= lines * WHEEL_UNIT
            self._scroll_by(lines)
        event.accept()

    # ------------------------------------------------------------ painting

    def _visible_rows(self) -> list:
        """The rows currently in view: a window over history + live screen,
        shifted up by self._scroll lines."""
        lines = self.screen.lines
        top = self.screen.history.top
        s = min(self._scroll, len(top))
        if not s:
            return [self.screen.buffer[y] for y in range(lines)]
        start = len(top) - s
        hist_part = list(islice(top, start, min(len(top), start + lines)))
        live_count = lines - len(hist_part)
        return hist_part + [self.screen.buffer[y] for y in range(live_count)]

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(DEFAULT_BG))
        painter.setFont(self._font)

        rows = self._visible_rows()
        for y, row in enumerate(rows):
            col = 0
            while col < self.screen.columns:
                cell = row[col]
                run_text = cell.data
                run_start = col
                fg, bg, bold, reverse = cell.fg, cell.bg, cell.bold, cell.reverse
                col += 1
                while (col < self.screen.columns
                       and row[col].fg == fg and row[col].bg == bg
                       and row[col].bold == bold and row[col].reverse == reverse):
                    run_text += row[col].data
                    col += 1
                self._paint_run(painter, run_start, y, run_text,
                                 fg, bg, bold, reverse)

        if not self._scroll and not self.screen.cursor.hidden and self.hasFocus():
            buf = self.screen.buffer
            cx, cy = self.screen.cursor.x, self.screen.cursor.y
            painter.fillRect(PAD + cx * self._char_w, PAD + cy * self._char_h,
                              self._char_w, self._char_h, QColor(CURSOR_COLOR))
            ch = buf[cy][cx].data
            if ch and ch != " ":
                painter.setPen(QColor(DEFAULT_BG))
                painter.drawText(PAD + cx * self._char_w,
                                  PAD + cy * self._char_h + self._char_h - 4, ch)

        if self._scroll:
            self._paint_scroll_thumb(painter)
        painter.end()

    def _paint_scroll_thumb(self, painter) -> None:
        top_len = len(self.screen.history.top)
        total = top_len + self.screen.lines
        h = self.height()
        thumb_h = max(24, round(h * self.screen.lines / total))
        frac = (top_len - self._scroll) / max(1, top_len)
        y = round(frac * (h - thumb_h))
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(SCROLLBAR_COLOR))
        painter.drawRoundedRect(self.width() - 6, y, 4, thumb_h, 2, 2)

    def _paint_run(self, painter, col, row, text, fg, bg, bold, reverse) -> None:
        x = PAD + col * self._char_w
        y = PAD + row * self._char_h
        if not text.strip():
            if bg != "default":
                bg_color = _resolve_color(bg, DEFAULT_BG)
                painter.fillRect(x, y, len(text) * self._char_w, self._char_h,
                                  bg_color)
            return
        fg_color = _resolve_color(fg, DEFAULT_FG)
        bg_color = _resolve_color(bg, DEFAULT_BG)
        if reverse:
            fg_color, bg_color = bg_color, fg_color
        if bg != "default" or reverse:
            painter.fillRect(x, y, len(text) * self._char_w, self._char_h,
                              bg_color)
        f = QFont(self._font)
        f.setBold(bold)
        painter.setFont(f)
        painter.setPen(fg_color)
        painter.drawText(x, y + self._char_h - 4, text)

    # ------------------------------------------------------------ input

    def keyPressEvent(self, event) -> None:
        mods = event.modifiers()
        key = event.key()

        # scrollback paging stays local; never reaches the shell
        if mods & Qt.ShiftModifier and key in (Qt.Key_PageUp, Qt.Key_PageDown):
            step = self.screen.lines - 1
            self._scroll_by(step if key == Qt.Key_PageUp else -step)
            return

        if not self.is_alive():
            return
        if self._scroll:            # typing snaps back to the live view
            self._scroll = 0
            self.update()

        if mods & Qt.ControlModifier and Qt.Key_A <= key <= Qt.Key_Z:
            self._write(chr(key - Qt.Key_A + 1))
            return
        if key in SPECIAL_KEYS:
            self._write(SPECIAL_KEYS[key])
            return
        text = event.text()
        if text:
            self._write(text)

    def _write(self, s: str) -> None:
        try:
            self.pty.write(s)
        except (OSError, winpty.WinptyError):
            pass
