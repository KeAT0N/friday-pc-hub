"""
dashboard/install_shortcut.py — create a Desktop shortcut for the dashboard.

    python -m dashboard.install_shortcut

Renders dashboard/icon.ico (offscreen Qt, no window flashes) and writes
"Remote Hub Dashboard.lnk" to the Desktop pointing at pythonw.exe (no console
window) with the repo as the working directory. Re-run any time; it
overwrites the existing shortcut.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
ICON = Path(__file__).resolve().parent / "icon.ico"


def render_icon() -> bool:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    try:
        from PySide6.QtGui import QGuiApplication

        from dashboard.theme import make_app_icon
    except ImportError:
        return False
    _app = QGuiApplication.instance() or QGuiApplication([])
    pixmap = make_app_icon(256).pixmap(256, 256)
    return pixmap.save(str(ICON), "ICO")


def main() -> None:
    import win32com.client

    pythonw = Path(sys.executable).with_name("pythonw.exe")
    target = pythonw if pythonw.exists() else Path(sys.executable)

    shell = win32com.client.Dispatch("WScript.Shell")
    desktop = shell.SpecialFolders("Desktop")  # handles OneDrive-moved Desktops
    lnk_path = str(Path(desktop) / "Remote Hub Dashboard.lnk")

    icon_ok = render_icon()
    lnk = shell.CreateShortcut(lnk_path)
    lnk.TargetPath = str(target)
    lnk.Arguments = "-m dashboard.main"
    lnk.WorkingDirectory = str(REPO)
    lnk.IconLocation = f"{ICON},0" if icon_ok else f"{target},0"
    lnk.Description = "Remote Hub Dashboard — terminal + app & hub controls"
    lnk.Save()

    print(f"shortcut: {lnk_path}")
    print(f"target:   {target} -m dashboard.main")
    print(f"icon:     {'generated ' + str(ICON) if icon_ok else 'fallback (python icon)'}")


if __name__ == "__main__":
    main()
