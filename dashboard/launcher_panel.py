"""
dashboard/launcher_panel.py — one-click app launch buttons.

Calls hub.apps._launch_target() in-process (the same alias-resolving launch
path `hub apps open <name>` uses), so behaviour matches the CLI exactly
without paying subprocess overhead per click.
"""

from __future__ import annotations

import subprocess

from PySide6.QtWidgets import QFrame, QGridLayout, QLabel, QMessageBox, QVBoxLayout, QPushButton

from hub.apps import APP_ALIASES, _launch_target

# button text -> alias key looked up in APP_ALIASES
BUTTONS = [
    ("🎮 Fortnite", "epic"), ("🌐 Chrome", "chrome"), ("💬 Discord", "discord"),
    ("🎵 Spotify", "spotify"), ("🕹️ Steam", "steam"), ("💻 VS Code", "vscode"),
    ("⌨️ Terminal", "terminal"), ("📁 Explorer", "files"), ("📝 Notepad", "notepad"),
]


class LauncherPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("kind", "section")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 14)
        lay.setSpacing(10)
        header = QLabel("APPS")
        header.setProperty("kind", "sectionLabel")
        lay.addWidget(header)

        grid = QGridLayout()
        grid.setSpacing(8)
        cols = 3
        for i, (label, alias) in enumerate(BUTTONS):
            btn = QPushButton(label)
            btn.setProperty("kind", "app")
            btn.clicked.connect(lambda _, a=alias, l=label: self._open(a, l))
            grid.addWidget(btn, i // cols, i % cols)
        lay.addLayout(grid)

    def _open(self, alias: str, label: str) -> None:
        target = APP_ALIASES.get(alias, alias)
        try:
            _launch_target(target)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            QMessageBox.warning(self, "Launch failed",
                                 f"Could not open {label}: {e}")
