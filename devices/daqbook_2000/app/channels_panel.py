"""Channels panel — per-channel configuration table.

Edits apply to the config immediately; a (re)connect pushes them to the
hardware.  Mirrors the rig's LabVIEW TDAQ CH table: channel number, name,
input mode, requested voltage range, plus the engineering-unit calibration
(units = volts × scale + offset).
"""

from __future__ import annotations

from typing import List

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QTableWidget, QVBoxLayout, QWidget,
)

from daqbook_2000 import theme
from daqbook_2000.config import ChannelConfig, DaqbookConfig
from daqbook_2000.daqx import range_for

_HEADERS = ["On", "CH", "Name", "Mode", "Min V", "Max V",
            "Native range", "Scale (unit/V)", "Offset", "Unit"]


class ChannelsPanel(QWidget):
    channelsChanged = pyqtSignal()

    def __init__(self, cfg: DaqbookConfig, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._build()
        self._populate()

    def _build(self):
        root = QVBoxLayout(self)
        hint = QLabel("Channel changes take effect on the next Connect. "
                      "Native range is the smallest hardware range covering "
                      "Min/Max V.")
        hint.setObjectName("dim")
        root.addWidget(hint)

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        root.addWidget(self.table, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        add = QPushButton("Add channel")
        add.clicked.connect(self._add_channel)
        row.addWidget(add)
        rem = QPushButton("Remove selected")
        rem.clicked.connect(self._remove_selected)
        row.addWidget(rem)
        root.addLayout(row)

    # ── table construction ──
    def _populate(self):
        self.table.setRowCount(0)
        for ch in self._cfg.channels:
            self._append_row(ch)

    def _append_row(self, ch: ChannelConfig):
        r = self.table.rowCount()
        self.table.insertRow(r)

        on = QCheckBox()
        on.setChecked(ch.enabled)
        on.toggled.connect(lambda v, c=ch: self._set(c, "enabled", v))
        wrap = QWidget()
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(6, 0, 0, 0)
        wl.addWidget(on)
        wl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.table.setCellWidget(r, 0, wrap)

        chan = self._spin_int(ch.channel, 0, 271)
        chan.valueChanged.connect(lambda v, c=ch: self._set(c, "channel", v))
        self.table.setCellWidget(r, 1, chan)

        name = QLineEdit(ch.name)
        name.textChanged.connect(lambda v, c=ch: self._set(c, "name", v))
        self.table.setCellWidget(r, 2, name)

        mode = QComboBox()
        mode.addItems(["Single-ended", "Differential"])
        mode.setCurrentIndex(1 if ch.differential else 0)
        mode.currentIndexChanged.connect(
            lambda i, c=ch: self._set(c, "differential", bool(i)))
        self.table.setCellWidget(r, 3, mode)

        vmin = self._spin_dbl(ch.v_min, -10.0, 10.0, 2)
        vmin.valueChanged.connect(
            lambda v, c=ch, row=r: self._range_changed(c, "v_min", v, row))
        self.table.setCellWidget(r, 4, vmin)

        vmax = self._spin_dbl(ch.v_max, -10.0, 10.0, 2)
        vmax.valueChanged.connect(
            lambda v, c=ch, row=r: self._range_changed(c, "v_max", v, row))
        self.table.setCellWidget(r, 5, vmax)

        native = QLabel(self._native_text(ch))
        native.setProperty("mono", "true")
        native.setStyleSheet(f"color: {theme.ACCENT_LIGHT}; padding-left: 6px;")
        self.table.setCellWidget(r, 6, native)

        scale = self._spin_dbl(ch.scale, -1e6, 1e6, 6)
        scale.valueChanged.connect(lambda v, c=ch: self._set(c, "scale", v))
        self.table.setCellWidget(r, 7, scale)

        offset = self._spin_dbl(ch.offset, -1e6, 1e6, 6)
        offset.valueChanged.connect(lambda v, c=ch: self._set(c, "offset", v))
        self.table.setCellWidget(r, 8, offset)

        unit = QLineEdit(ch.unit)
        unit.textChanged.connect(lambda v, c=ch: self._set(c, "unit", v))
        self.table.setCellWidget(r, 9, unit)

    @staticmethod
    def _native_text(ch: ChannelConfig) -> str:
        gain, bipolar = ch.gain_bipolar
        lo, hi = range_for(gain, bipolar)
        return f"{lo:+.2f}..{hi:+.2f} V (×{gain})"

    @staticmethod
    def _spin_int(value, lo, hi):
        from PyQt6.QtWidgets import QSpinBox
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setValue(value)
        return sp

    @staticmethod
    def _spin_dbl(value, lo, hi, decimals):
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setDecimals(decimals)
        sp.setValue(value)
        return sp

    # ── edits ──
    def _set(self, ch: ChannelConfig, attr: str, value) -> None:
        setattr(ch, attr, value)
        self.channelsChanged.emit()

    def _range_changed(self, ch: ChannelConfig, attr: str, value,
                       row: int) -> None:
        setattr(ch, attr, value)
        w = self.table.cellWidget(row, 6)
        if isinstance(w, QLabel):
            w.setText(self._native_text(ch))
        self.channelsChanged.emit()

    def _add_channel(self):
        used = {c.channel for c in self._cfg.channels}
        nxt = next(i for i in range(272) if i not in used)
        ch = ChannelConfig(channel=nxt, name=f"CH{nxt}", enabled=False)
        self._cfg.channels.append(ch)
        self._append_row(ch)
        self.channelsChanged.emit()

    def _remove_selected(self):
        rows = sorted({i.row() for i in self.table.selectedIndexes()},
                      reverse=True)
        for r in rows:
            if 0 <= r < len(self._cfg.channels):
                del self._cfg.channels[r]
                self.table.removeRow(r)
        if rows:
            self.channelsChanged.emit()

    def reload(self):
        self._populate()
