"""Diagnostics panel — raw ControlWord + 750-673 module status.

Shows the raw ControlWord echo live (hex + bits, with the bit legend
from the PLC source) and the per-axis 750-673 stepper-module status
bytes, with a timestamped log of every S1 transition (position + time).
A faulting start on the rig identifies the module's error bit here.
"""

from __future__ import annotations

import time
from typing import Dict

from PyQt6.QtWidgets import (
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from traverse_swt import theme
from traverse_swt.config import TraverseConfig
from traverse_swt.device import TraverseDrive

_CONTROL_LEGEND = "bit0 X-fwd  bit1 X-rev  bit2/3 Y-fwd/rev  bit4/5 Z-fwd/rev"


def _bits(word: int, n: int = 9) -> str:
    return " ".join(str((word >> b) & 1) for b in range(n - 1, -1, -1))


class DiagnosticsPanel(QWidget):
    def __init__(self, cfg: TraverseConfig, device: TraverseDrive,
                 parent=None):
        super().__init__(parent)
        self.config = cfg
        self._device = device

        root = QVBoxLayout(self)
        root.setSpacing(8)

        words = QGroupBox("Raw PLC words (wire 12288)")
        wg = QGridLayout(words)
        wg.addWidget(QLabel("ControlWord echo"), 0, 0)
        self.ctrl_lbl = QLabel("--")
        self.ctrl_lbl.setProperty("mono", "true")
        self.ctrl_lbl.setStyleSheet(f"color: {theme.ACCENT_LIGHT}; "
                                    f"font-size: 12pt;")
        wg.addWidget(self.ctrl_lbl, 0, 1)
        leg1 = QLabel(_CONTROL_LEGEND)
        leg1.setObjectName("dim")
        wg.addWidget(leg1, 0, 2)

        wg.addWidget(QLabel("750-673 module status (S1·S2·S3)"), 1, 0)
        self.mod_lbl = QLabel("--")
        self.mod_lbl.setProperty("mono", "true")
        self.mod_lbl.setStyleSheet(f"color: {theme.ACCENT_LIGHT};")
        self.mod_lbl.setToolTip(
            "Raw stepper-module status bytes from the input image. "
            "Bit meanings are undocumented in the extracted source — "
            "S1 transitions are logged below; the byte that changes "
            "when a start faults is the error flag.")
        wg.addWidget(self.mod_lbl, 1, 1, 1, 2)
        root.addWidget(words)

        logbox = QGroupBox("Module status event log")
        lv = QVBoxLayout(logbox)
        head = QHBoxLayout()
        head.addStretch(1)
        clear_btn = QPushButton("Clear log")
        clear_btn.clicked.connect(self._clear_log)
        head.addWidget(clear_btn)
        lv.addLayout(head)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Time", "Axis", "Event", "Counts"])
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setColumnWidth(0, 110)
        self.table.setColumnWidth(1, 50)
        self.table.setColumnWidth(2, 200)
        lv.addWidget(self.table, 1)
        root.addWidget(logbox, 1)

        hint = QLabel(
            "Watch S1 when a start faults: the byte that changes at a "
            "failed start is the module's error flag (undocumented in "
            "the extracted CoDeSys source). Frozen counts while an axis "
            "is commanded raise a STALL — the module is not stepping.")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        root.addWidget(hint)

    # ── live updates ──
    def append_module(self, ev: tuple) -> None:
        """One 750-673 S1 status transition: (t, axis, old, new, counts)."""
        t, axis, old, new, counts = ev
        row = self.table.rowCount()
        self.table.insertRow(row)
        ts = time.strftime("%H:%M:%S", time.localtime(t))
        ts += f".{int((t % 1) * 10)}"
        vals = [ts, axis, f"MODULE S1 0x{old:02X}→0x{new:02X}",
                f"{counts:+d}"]
        for col, v in enumerate(vals):
            self.table.setItem(row, col, QTableWidgetItem(v))
        self.table.scrollToBottom()

    def refresh(self, control: int, state: Dict[str, dict]) -> None:
        self.ctrl_lbl.setText(f"0x{control:04X}  [{_bits(control)}]")
        parts = []
        for name in "XYZ":
            st = state.get(name)
            if st is None:
                continue
            s1, s2, s3 = st["module_status"]
            parts.append(f"{name}: {s1:02X}·{s2:02X}·{s3:02X}")
        self.mod_lbl.setText("    ".join(parts) if parts else "--")

    def _clear_log(self):
        self.table.setRowCount(0)
