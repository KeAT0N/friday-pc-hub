"""
dashboard/theme.py — the dashboard's dark theme.

One QSS sheet + small helpers so every panel styles itself the same way:
`apply_theme()` on the QApplication, `make_section()` for the rounded sidebar
cards, `enable_dark_titlebar()` for the native Win11 dark title bar.
"""

from __future__ import annotations

import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import (QBrush, QColor, QFont, QIcon, QLinearGradient,
                             QPainter, QPixmap)
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout

ACCENT_BLUE = "#3b82f6"
ACCENT_PURPLE = "#8b5cf6"
ACCENT_AMBER = "#f59e0b"
GREEN = "#34d399"
RED = "#f87171"

QSS = """
QWidget { font-family: "Segoe UI"; font-size: 10pt; color: #c9d5e3; }
#root { background-color: #0a0e14; }
#sidebar { background-color: #0d1320; border-left: 1px solid #1a2433; }

#termFrame { background-color: #0c1016; border: 1px solid #1a2433;
             border-radius: 12px; }
QLabel[kind="termHeader"] { color: #44566e; font-size: 8.5pt;
                             font-weight: 600; background: transparent; }

QLabel#appTitle { color: #eef4fb; background: transparent; }
QLabel#appSub { color: #44566e; background: transparent; }
QLabel[kind="sectionLabel"] { color: #5a7191; font-size: 8.5pt;
                               font-weight: 700; background: transparent; }
QLabel[kind="footer"] { color: #3d4e66; font-size: 8.5pt;
                         background: transparent; }

QFrame[kind="section"] { background-color: #0f1626; border: 1px solid #1a2433;
                          border-radius: 12px; }
QFrame[kind="card"] { background-color: #111a2c; border: 1px solid #1e2a40;
                       border-radius: 10px; }
QLabel[kind="statValue"] { color: #eef4fb; font-size: 13pt; font-weight: 700;
                            background: transparent; border: none; }
QLabel[kind="statName"] { color: #5a7191; font-size: 8pt; font-weight: 600;
                           background: transparent; border: none; }

QProgressBar { background-color: #0a0f1a; border: none; border-radius: 2px;
               max-height: 4px; }
QProgressBar::chunk { background-color: %(blue)s; border-radius: 2px; }

QPushButton { background-color: #131d30; color: #c9d5e3;
              border: 1px solid #223048; border-radius: 9px;
              padding: 9px 10px; font-weight: 600; }
QPushButton:hover { background-color: #182742; border-color: #2f4266;
                    color: #ffffff; }
QPushButton:pressed { background-color: #0f1930; }
QPushButton[kind="app"]:hover { border-color: %(blue)s; }
QPushButton[kind="power"]:hover { border-color: %(amber)s; }
QPushButton[kind="friday"]:hover { border-color: %(purple)s; }

QSplitter::handle { background-color: #0a0e14; }
QMenu { background-color: #111a2c; color: #c9d5e3;
        border: 1px solid #223048; border-radius: 8px; padding: 4px; }
QMenu::item { padding: 6px 18px; border-radius: 6px; }
QMenu::item:selected { background-color: #1c2c4a; }
QMessageBox { background-color: #0f1626; }
QMessageBox QLabel { color: #c9d5e3; }
QToolTip { background-color: #111a2c; color: #c9d5e3;
           border: 1px solid #223048; }
""" % {"blue": ACCENT_BLUE, "purple": ACCENT_PURPLE, "amber": ACCENT_AMBER}


def apply_theme(app) -> None:
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    app.setFont(QFont("Segoe UI", 10))


def make_section(title: str) -> tuple[QFrame, QVBoxLayout]:
    """A rounded sidebar card with a small uppercase header label."""
    frame = QFrame()
    frame.setProperty("kind", "section")
    lay = QVBoxLayout(frame)
    lay.setContentsMargins(14, 12, 14, 14)
    lay.setSpacing(10)
    label = QLabel(title.upper())
    label.setProperty("kind", "sectionLabel")
    lay.addWidget(label)
    return frame, lay


def enable_dark_titlebar(widget) -> None:
    """Native Win11 dark title bar (DWMWA_USE_IMMERSIVE_DARK_MODE). Fail-soft
    everywhere else."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        hwnd = int(widget.winId())
        value = ctypes.c_int(1)
        for attr in (20, 19):  # 19 on pre-20H1 builds
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
    except (OSError, AttributeError):
        pass


def make_app_icon(size: int = 256) -> QIcon:
    """The blue→purple 'H' tile, generated so there's no asset dependency."""
    pix = QPixmap(size, size)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    grad = QLinearGradient(0, 0, size, size)
    grad.setColorAt(0, QColor(ACCENT_BLUE))
    grad.setColorAt(1, QColor(ACCENT_PURPLE))
    p.setBrush(QBrush(grad))
    p.setPen(Qt.NoPen)
    m = round(size * 0.03)
    r = round(size * 0.2)
    p.drawRoundedRect(m, m, size - 2 * m, size - 2 * m, r, r)
    f = QFont("Segoe UI", round(size * 0.43))
    f.setWeight(QFont.Black)
    p.setFont(f)
    p.setPen(QColor("#ffffff"))
    p.drawText(pix.rect().adjusted(0, 0, 0, -round(size * 0.02)),
                Qt.AlignCenter, "H")
    p.end()
    return QIcon(pix)
