"""
dashboard/stats_panel.py — live system stats + hub status for the sidebar.

CPU / RAM / battery-or-disk cards refreshed on a 2s QTimer (psutil is already
a hub dependency), plus a hub-readiness line derived from the same
assert_alive() kill-switch gate every module CLI uses.
"""

from __future__ import annotations

import psutil
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QProgressBar,
                                 QVBoxLayout)

from dashboard.theme import GREEN, RED
from hub.safety import KillSwitchEngaged, assert_alive

REFRESH_MS = 2000


class StatCard(QFrame):
    def __init__(self, name: str, parent=None):
        super().__init__(parent)
        self.setProperty("kind", "card")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 10)
        lay.setSpacing(4)
        self.value_label = QLabel("--")
        self.value_label.setProperty("kind", "statValue")
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(False)
        self.name_label = QLabel(name)
        self.name_label.setProperty("kind", "statName")
        lay.addWidget(self.value_label)
        lay.addWidget(self.bar)
        lay.addWidget(self.name_label)

    def set(self, percent: float | None, text: str | None = None,
            name: str | None = None) -> None:
        if percent is None:
            self.value_label.setText("--")
            self.bar.setValue(0)
        else:
            self.value_label.setText(text or f"{percent:.0f}%")
            self.bar.setValue(round(percent))
        if name:
            self.name_label.setText(name)


class StatsPanel(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("kind", "section")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 14)
        lay.setSpacing(10)
        header = QLabel("SYSTEM")
        header.setProperty("kind", "sectionLabel")
        lay.addWidget(header)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.cpu = StatCard("CPU")
        self.ram = StatCard("RAM")
        self.aux = StatCard("BATTERY")
        for card in (self.cpu, self.ram, self.aux):
            row.addWidget(card, 1)
        lay.addLayout(row)

        self.status = QLabel()
        lay.addWidget(self.status)

        psutil.cpu_percent(None)  # prime the sampler; first real read next tick
        self._timer = QTimer(self)
        self._timer.timeout.connect(self.refresh)
        self._timer.start(REFRESH_MS)
        self.refresh()

    def refresh(self) -> None:
        self.cpu.set(psutil.cpu_percent(None))
        self.ram.set(psutil.virtual_memory().percent)

        battery = None
        try:
            battery = psutil.sensors_battery()
        except (AttributeError, OSError):
            pass
        if battery is not None and battery.percent is not None:
            self.aux.set(battery.percent, name="BATTERY")
        else:
            self.aux.set(psutil.disk_usage("C:\\").percent, name="DISK C:")

        try:
            assert_alive("dashboard.status")
            self.status.setText("●  Hub ready")
            self.status.setStyleSheet(
                f"color: {GREEN}; font-weight: 600; background: transparent;")
        except KillSwitchEngaged:
            self.status.setText("●  KILL-SWITCH ENGAGED")
            self.status.setStyleSheet(
                f"color: {RED}; font-weight: 600; background: transparent;")
