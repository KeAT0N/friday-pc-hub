"""
dashboard/hub_panel.py — buttons for hub power actions and FRIDAY scenes.

Power actions (lock/monitor-off/sleep) call hub.power's effect helpers
directly in-process, behind the same assert_alive() kill-switch gate the CLI
uses, plus a Qt confirmation dialog (the GUI click IS the human confirmation
that --confirm represents on the CLI). FRIDAY scenes are shelled out via
subprocess instead: friday.py's dispatch path calls sys.exit() internally,
which would kill this GUI process if imported and called in-process.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtWidgets import (QFrame, QGridLayout, QLabel, QMessageBox,
                                 QPushButton, QVBoxLayout)

from hub import power
from hub.safety import KillSwitchEngaged, assert_alive

REPO = Path(__file__).resolve().parent.parent

# label -> (power.py verb, needs an "are you sure?" confirm dialog)
POWER_BUTTONS = [
    ("🔒 Lock", "lock", False),
    ("🖥️ Monitor Off", "monitor-off", False),
    ("😴 Sleep", "sleep", True),
]

# label -> friday scene name, all confirmed (scenes wipe windows / power down)
SCENE_BUTTONS = [
    ("🪑 Sit Down", "sit-down"), ("🛋️ Chill", "chill"),
    ("⚡ Optimize", "optimize"), ("🌙 Goodnight", "goodnight"),
]


class HubPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("kind", "section")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 14)
        lay.setSpacing(10)

        power_header = QLabel("POWER")
        power_header.setProperty("kind", "sectionLabel")
        lay.addWidget(power_header)

        power_grid = QGridLayout()
        power_grid.setSpacing(8)
        for col, (label, verb, confirm) in enumerate(POWER_BUTTONS):
            btn = QPushButton(label)
            btn.setProperty("kind", "power")
            btn.clicked.connect(
                lambda _, v=verb, l=label, c=confirm: self._power(v, l, c))
            power_grid.addWidget(btn, 0, col)
        lay.addLayout(power_grid)

        scene_header = QLabel("FRIDAY SCENES")
        scene_header.setProperty("kind", "sectionLabel")
        lay.addWidget(scene_header)

        scene_grid = QGridLayout()
        scene_grid.setSpacing(8)
        for i, (label, scene) in enumerate(SCENE_BUTTONS):
            btn = QPushButton(label)
            btn.setProperty("kind", "friday")
            btn.clicked.connect(lambda _, s=scene, l=label: self._trigger(s, l))
            scene_grid.addWidget(btn, i // 2, i % 2)
        lay.addLayout(scene_grid)

    def _power(self, verb: str, label: str, confirm: bool) -> None:
        if confirm and not self._confirm(f"{label}?", power.PLANS[verb]):
            return
        try:
            assert_alive(f"dashboard.power.{verb}")
            power.EFFECTS[verb](None)
        except KillSwitchEngaged as e:
            QMessageBox.warning(self, "Blocked", str(e))
        except OSError as e:
            QMessageBox.warning(self, f"{label} failed", str(e))

    def _trigger(self, scene: str, label: str) -> None:
        if not self._confirm(f"Run FRIDAY scene '{scene}'?",
                              "This may close windows, launch apps, or change power state."):
            return
        try:
            subprocess.Popen(
                [sys.executable, "-m", "hub.friday", "trigger", scene],
                cwd=str(REPO),
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except OSError as e:
            QMessageBox.warning(self, f"{label} failed", str(e))

    def _confirm(self, title: str, detail: str) -> bool:
        box = QMessageBox(QMessageBox.Question, title, detail,
                           QMessageBox.Yes | QMessageBox.No, self)
        return box.exec() == QMessageBox.Yes
