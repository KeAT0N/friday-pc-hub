"""
dashboard/main.py — always-open Remote Hub dashboard.

    python -m dashboard.main            (or the Desktop shortcut)

A single window: an embedded terminal on the left; live system stats, app
launchers, and hub controls on the right. Closing the window minimizes it to
a system tray icon instead of quitting (that's the "always open" part) — use
the tray icon's context menu to actually quit. A QLockFile makes the app
single-instance so a double-click while it's already running can't spawn a
second tray icon.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PySide6.QtCore import QDir, QLockFile, Qt
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (QApplication, QFrame, QLabel, QMainWindow,
                                 QMenu, QMessageBox, QSplitter, QSystemTrayIcon,
                                 QVBoxLayout, QWidget)

from dashboard.hub_panel import HubPanel
from dashboard.launcher_panel import LauncherPanel
from dashboard.stats_panel import StatsPanel
from dashboard.terminal_widget import TerminalWidget
from dashboard.theme import apply_theme, enable_dark_titlebar, make_app_icon

ICON_FILE = Path(__file__).with_name("icon.ico")


class DashboardWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Remote Hub Dashboard")
        self._icon = (QIcon(str(ICON_FILE)) if ICON_FILE.exists()
                      else make_app_icon())
        self.setWindowIcon(self._icon)
        self.resize(1280, 720)
        self.setMinimumSize(900, 560)

        root = QWidget()
        root.setObjectName("root")

        # left: terminal in a rounded frame with a small header
        term_frame = QFrame()
        term_frame.setObjectName("termFrame")
        term_lay = QVBoxLayout(term_frame)
        term_lay.setContentsMargins(12, 10, 12, 12)
        term_lay.setSpacing(6)
        term_header = QLabel("TERMINAL — POWERSHELL")
        term_header.setProperty("kind", "termHeader")
        self.terminal = TerminalWidget()
        term_lay.addWidget(term_header)
        term_lay.addWidget(self.terminal, 1)

        # right: sidebar
        sidebar = QWidget()
        sidebar.setObjectName("sidebar")
        sidebar.setMinimumWidth(340)
        sidebar.setMaximumWidth(460)
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(16, 18, 16, 12)
        side.setSpacing(12)

        title = QLabel("REMOTE HUB")
        title.setObjectName("appTitle")
        title_font = QFont("Segoe UI", 15)
        title_font.setWeight(QFont.Black)
        title_font.setLetterSpacing(QFont.PercentageSpacing, 112)
        title.setFont(title_font)
        subtitle = QLabel("CONTROL DECK")
        subtitle.setObjectName("appSub")
        sub_font = QFont("Segoe UI", 8)
        sub_font.setWeight(QFont.DemiBold)
        sub_font.setLetterSpacing(QFont.PercentageSpacing, 160)
        subtitle.setFont(sub_font)
        side.addWidget(title)
        side.addWidget(subtitle)

        side.addWidget(StatsPanel())
        side.addWidget(LauncherPanel())
        side.addWidget(HubPanel())
        side.addStretch(1)

        footer = QLabel("closes to tray  ·  right-click the tray icon to quit")
        footer.setProperty("kind", "footer")
        footer.setAlignment(Qt.AlignHCenter)
        side.addWidget(footer)

        splitter = QSplitter(Qt.Horizontal, root)
        splitter.setHandleWidth(8)
        splitter.addWidget(term_frame)
        splitter.addWidget(sidebar)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setSizes([880, 400])

        root_lay = QVBoxLayout(root)
        root_lay.setContentsMargins(10, 10, 0, 10)
        root_lay.addWidget(splitter)
        self.setCentralWidget(root)

        self._build_tray()

    def _build_tray(self) -> None:
        self.tray = QSystemTrayIcon(self._icon, self)
        self.tray.setToolTip("Remote Hub Dashboard")
        menu = QMenu()
        show_action = menu.addAction("Show dashboard")
        show_action.triggered.connect(self._restore)
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self._quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._on_tray_activated)
        self.tray.show()

    def _on_tray_activated(self, reason) -> None:
        if reason == QSystemTrayIcon.Trigger:
            self._restore()

    def _restore(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, event) -> None:
        # "Always open": closing the window hides it to the tray rather than
        # exiting, so the shell/PTY session and its state stay alive.
        event.ignore()
        self.hide()
        self.tray.showMessage("Remote Hub Dashboard", "Still running in the tray.",
                               QSystemTrayIcon.Information, 2000)

    def _quit(self) -> None:
        self.terminal.shutdown()
        QApplication.instance().quit()


def main() -> None:
    app = QApplication(sys.argv)
    apply_theme(app)
    app.setQuitOnLastWindowClosed(False)

    lock = QLockFile(QDir.tempPath() + "/remote-hub-dashboard.lock")
    if not lock.tryLock(100):
        QMessageBox.information(
            None, "Remote Hub Dashboard",
            "The dashboard is already running — look for the blue H icon "
            "in the system tray.")
        sys.exit(0)

    window = DashboardWindow()
    window.show()
    enable_dark_titlebar(window)
    exit_code = app.exec()
    del lock  # released on process exit anyway; explicit for clarity
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
