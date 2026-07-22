"""Channels panel — per-channel AI configuration table.

Edits apply to the config immediately; a (re)connect pushes them to the
hardware. The 6351 has 16 AI channels and the set is reconfigurable, so
rows can be added/removed. Bridge channels record raw volts (the balance
``.vol`` calibration converts to forces in AeroVIS); extra channels may
carry real EU slopes via Scale/Offset/Unit.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QPushButton, QSpinBox, QTableWidget, QVBoxLayout, QWidget,
)

from ni_usb_6351.config import (
    AI_RANGES_V, TERMINALS, ChannelConfig, NiDaqConfig,
)

_HEADERS = ["On", "AI", "Name", "Terminal", "Range", "Balance",
            "Scale", "Offset", "Unit"]


def _range_label(v: float) -> str:
    if v < 1.0:
        return f"±{v * 1000:g} mV"
    return f"±{v:g} V"


class ChannelsPanel(QWidget):
    channelsChanged = pyqtSignal()

    def __init__(self, cfg: NiDaqConfig, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._build()
        self._populate()

    def _build(self):
        root = QVBoxLayout(self)

        hint = QLabel(
            "Channel changes take effect on the next Connect. Balance "
            "channels record raw volts — bridge volts → forces happens in "
            "AeroVIS via the balance .vol calibration. Range picks the "
            "nearest native ± input range of the X-series front end.")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        for col in (0, 1, 5):              # On, AI, Balance
            self.table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        root.addWidget(self.table, 1)

        bar = QHBoxLayout()
        self.add_btn = QPushButton("Add channel")
        self.add_btn.clicked.connect(self._add_channel)
        bar.addWidget(self.add_btn)
        self.remove_btn = QPushButton("Remove selected")
        self.remove_btn.clicked.connect(self._remove_selected)
        bar.addWidget(self.remove_btn)
        bar.addStretch(1)
        root.addLayout(bar)

    # ── table ──
    def _populate(self):
        self.table.setRowCount(0)
        for ch in self._cfg.channels:
            self._append_row(ch)

    def _append_row(self, ch: ChannelConfig):
        r = self.table.rowCount()
        self.table.insertRow(r)

        self.table.setCellWidget(r, 0, self._check(
            ch.enabled, lambda v, c=ch: self._set(c, "enabled", v)))

        sp = QSpinBox()
        sp.setRange(0, 15)
        sp.setPrefix("ai")
        sp.setValue(ch.channel)
        sp.valueChanged.connect(lambda v, c=ch: self._set(c, "channel", v))
        self.table.setCellWidget(r, 1, sp)

        name = QLineEdit(ch.name)
        name.textChanged.connect(lambda v, c=ch: self._set(c, "name", v))
        self.table.setCellWidget(r, 2, name)

        term = QComboBox()
        term.addItems(list(TERMINALS))
        term.setCurrentText(ch.terminal)
        term.currentTextChanged.connect(
            lambda v, c=ch: self._set(c, "terminal", v))
        self.table.setCellWidget(r, 3, term)

        rng = QComboBox()
        for v in AI_RANGES_V:
            rng.addItem(_range_label(v), v)
        rng.setCurrentIndex(AI_RANGES_V.index(ch.native_range))
        rng.currentIndexChanged.connect(
            lambda i, c=ch, cb=rng: self._range_changed(c, cb.itemData(i)))
        self.table.setCellWidget(r, 4, rng)

        self.table.setCellWidget(r, 5, self._check(
            ch.balance, lambda v, c=ch: self._set(c, "balance", v)))

        scale = QDoubleSpinBox()
        scale.setRange(-1e9, 1e9)
        scale.setDecimals(6)
        scale.setValue(ch.scale)
        scale.valueChanged.connect(lambda v, c=ch: self._set(c, "scale", v))
        self.table.setCellWidget(r, 6, scale)

        offset = QDoubleSpinBox()
        offset.setRange(-1e9, 1e9)
        offset.setDecimals(6)
        offset.setValue(ch.offset)
        offset.valueChanged.connect(lambda v, c=ch: self._set(c, "offset", v))
        self.table.setCellWidget(r, 7, offset)

        unit = QLineEdit(ch.unit)
        unit.textChanged.connect(lambda v, c=ch: self._set(c, "unit", v))
        self.table.setCellWidget(r, 8, unit)

    @staticmethod
    def _check(value: bool, slot) -> QWidget:
        box = QCheckBox()
        box.setChecked(value)
        box.toggled.connect(slot)
        wrap = QWidget()
        wl = QHBoxLayout(wrap)
        wl.setContentsMargins(6, 0, 6, 0)
        wl.addWidget(box)
        wl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        return wrap

    def _set(self, ch: ChannelConfig, attr: str, value) -> None:
        setattr(ch, attr, value)
        self.channelsChanged.emit()

    def _range_changed(self, ch: ChannelConfig, value) -> None:
        r = float(value)
        ch.v_min, ch.v_max = -r, r
        self.channelsChanged.emit()

    # ── add / remove ──
    def _add_channel(self):
        used = {c.channel for c in self._cfg.channels}
        free = [i for i in range(16) if i not in used]
        ai = free[0] if free else 0
        ch = ChannelConfig(channel=ai, name=f"AI{ai}")
        self._cfg.channels.append(ch)
        self._append_row(ch)
        self.channelsChanged.emit()

    def _remove_selected(self):
        row = self.table.currentRow()
        if not (0 <= row < len(self._cfg.channels)):
            return
        del self._cfg.channels[row]
        self._populate()
        self.channelsChanged.emit()

    def reload(self):
        self._populate()
