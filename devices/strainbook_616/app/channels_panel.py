"""Channels panel — per-channel strain configuration table.

Mirrors the rig's LabVIEW "Additional StrainBook Parameters" table.  Edits
apply to the config immediately; a (re)connect pushes them to the hardware.
The bridges run on an EXTERNAL excitation supply, so there are no internal
excitation-bank controls — CH8 reads the external excitation back via the
"0 to 10 V" range.
"""

from __future__ import annotations

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QSpinBox, QTableWidget, QVBoxLayout, QWidget,
)

from strainbook_616 import daqx
from strainbook_616.config import StrainChannelConfig, StrainbookConfig

_HEADERS = ["On", "CH", "Name", "Bridge", "Range",
            "Filter", "AC", "Inv", "SSH", "Offset", "Unit"]

# Standard input ranges (the rig's LabVIEW dropdown list), ± mV
_STD_RANGES = [5000.0, 2500.0, 1000.0, 500.0, 235.0, 100.0, 50.0,
               32.0, 11.0, 5.0, 2.5, 1.0, 0.5, 0.25]

# Sentinel Range item = CH8 external-excitation readback (0..10 V).
_EXC_RANGE = "exc"
_EXC_LABEL = "0 to 10 V"


def _range_label(mv: float) -> str:
    if mv >= 1000.0:
        return f"±{mv / 1000:g} V"
    return f"±{mv:g} mV"


class ChannelsPanel(QWidget):
    channelsChanged = pyqtSignal()

    def __init__(self, cfg: StrainbookConfig, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._build()
        self._populate()

    def _build(self):
        root = QVBoxLayout(self)

        hint = QLabel(
            "Channel changes take effect on the next Connect. Bridge volts → "
            "forces happens in AeroVIS via the balance .vol calibration. Set a "
            "channel's Range to \"0 to 10 V\" to read the external excitation "
            "supply back (CH8).")
        hint.setObjectName("dim")
        hint.setWordWrap(True)
        root.addWidget(hint)

        self.table = QTableWidget(0, len(_HEADERS))
        self.table.setHorizontalHeaderLabels(_HEADERS)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        for col in (0, 1, 6, 7, 8):        # On, CH, AC, Inv, SSH
            self.table.horizontalHeader().setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.verticalHeader().setVisible(False)
        root.addWidget(self.table, 1)

    # ── table ──
    def _populate(self):
        self.table.setRowCount(0)
        for ch in self._cfg.channels:
            self._append_row(ch)

    def _append_row(self, ch: StrainChannelConfig):
        r = self.table.rowCount()
        self.table.insertRow(r)

        self.table.setCellWidget(r, 0, self._check(
            ch.enabled, lambda v, c=ch: self._set(c, "enabled", v)))

        sp = QSpinBox()
        sp.setRange(1, 72)
        sp.setValue(ch.channel)
        sp.valueChanged.connect(lambda v, c=ch: self._set(c, "channel", v))
        self.table.setCellWidget(r, 1, sp)

        name = QLineEdit(ch.name)
        name.textChanged.connect(lambda v, c=ch: self._set(c, "name", v))
        self.table.setCellWidget(r, 2, name)

        bridge = QComboBox()
        if ch.read_excitation:
            # excitation readback is configured as full-bridge completion
            bridge.addItem(daqx.BRIDGE_NAMES[ch.bridge])
            bridge.setEnabled(False)
        else:
            bridge.addItems([daqx.BRIDGE_NAMES[i] for i in (0, 1, 2)])
            bridge.setCurrentIndex(ch.bridge)
            bridge.currentIndexChanged.connect(
                lambda i, c=ch: self._set(c, "bridge", i))
        self.table.setCellWidget(r, 3, bridge)

        rng = QComboBox()
        rng.addItem(_EXC_LABEL, _EXC_RANGE)      # index 0 = excitation readback
        for mv in _STD_RANGES:
            rng.addItem(_range_label(mv), mv)
        if ch.read_excitation:
            rng.setCurrentIndex(0)
        else:
            nearest = min(range(len(_STD_RANGES)),
                          key=lambda i: abs(_STD_RANGES[i] - ch.range_mv))
            rng.setCurrentIndex(nearest + 1)     # +1 past the "0 to 10 V" item
        rng.currentIndexChanged.connect(
            lambda i, c=ch, row=r, cb=rng: self._range_changed(
                c, cb.itemData(i), row))
        self.table.setCellWidget(r, 4, rng)

        filt = QComboBox()
        filt.addItems([daqx.FILTER_NAMES[i] for i in (0, 1, 2)])
        filt.setCurrentIndex(ch.filter_type)
        filt.currentIndexChanged.connect(
            lambda i, c=ch: self._set(c, "filter_type", i))
        self.table.setCellWidget(r, 5, filt)

        self.table.setCellWidget(r, 6, self._check(
            ch.ac_couple, lambda v, c=ch: self._set(c, "ac_couple", v)))
        self.table.setCellWidget(r, 7, self._check(
            ch.invert, lambda v, c=ch: self._set(c, "invert", v)))
        self.table.setCellWidget(r, 8, self._check(
            ch.ssh, lambda v, c=ch: self._set(c, "ssh", v)))

        offset = QDoubleSpinBox()
        offset.setRange(-1e9, 1e9)
        offset.setDecimals(6)
        offset.setValue(ch.offset)
        offset.valueChanged.connect(lambda v, c=ch: self._set(c, "offset", v))
        self.table.setCellWidget(r, 9, offset)

        unit = QLineEdit(ch.unit)
        unit.textChanged.connect(lambda v, c=ch: self._set(c, "unit", v))
        self.table.setCellWidget(r, 10, unit)

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

    def _set(self, ch: StrainChannelConfig, attr: str, value) -> None:
        setattr(ch, attr, value)
        self.channelsChanged.emit()

    def _range_changed(self, ch: StrainChannelConfig, value, row: int):
        """Range is the single per-channel range control.

        Selecting "0 to 10 V" turns the channel into the external-excitation
        readback (read_excitation, ±5 V hardware, ×1, full-bridge, offset 0,
        unit V — the channel reports the excitation voltage verbatim).
        Selecting any ± mV range clears the readback and keeps the offset at
        0 for a normal channel. When the mode flips, the row is rebuilt so
        the Bridge/Offset/Unit cells match.
        """
        was_exc = ch.read_excitation
        if value == _EXC_RANGE:
            ch.read_excitation = True
            ch.range_mv = 5000.0
            ch.offset = 0.0
            ch.unit = "V"
        else:
            if ch.read_excitation:
                ch.read_excitation = False
                ch.offset = 0.0
            ch.range_mv = value
        self.channelsChanged.emit()
        if ch.read_excitation != was_exc:
            # defer so the QComboBox that fired this isn't deleted mid-signal
            QTimer.singleShot(0, self._populate)

    def reload(self):
        self._populate()
