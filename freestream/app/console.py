"""Bottom-dock run log — timestamped, read-only console."""

from __future__ import annotations

import time

from PyQt6.QtWidgets import QPlainTextEdit

from .. import theme


class ConsolePanel(QPlainTextEdit):
    """Read-only, timestamped event log (SweepCallbacks.on_event sink)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setMaximumBlockCount(5000)
        self.setStyleSheet(
            f"QPlainTextEdit {{ background-color: {theme.BG_LIGHT};"
            f" color: {theme.TEXT}; border: 1px solid {theme.BORDER};"
            f" font-family: Consolas, monospace; font-size: 9pt; }}")

    def log(self, msg: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.appendPlainText(f"[{stamp}] {msg}")
        sb = self.verticalScrollBar()
        sb.setValue(sb.maximum())
