"""Settings dialog — polling, protocol verification flags, write limits."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QGroupBox, QLabel, QLineEdit, QSpinBox, QVBoxLayout,
)

from tunnel_plc.config import WORD_ORDERS, TunnelConfig


class SettingsDialog(QDialog):
    def __init__(self, cfg: TunnelConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(540)
        self._cfg = cfg
        self._build(cfg)

    def _build(self, cfg: TunnelConfig):
        root = QVBoxLayout(self)

        mon = QGroupBox("Monitor")
        mf = QFormLayout(mon)
        self.poll_s = QDoubleSpinBox()
        self.poll_s.setRange(0.1, 10.0)
        self.poll_s.setDecimals(2)
        self.poll_s.setValue(cfg.poll_s)
        self.poll_s.setSuffix(" s")
        mf.addRow("Poll period", self.poll_s)
        self.stale_s = QDoubleSpinBox()
        self.stale_s.setRange(0.5, 60.0)
        self.stale_s.setDecimals(1)
        self.stale_s.setValue(cfg.stale_after_s)
        self.stale_s.setSuffix(" s")
        self.stale_s.setToolTip("Snapshot older than this = STALE; "
                                "guarded writes refuse")
        mf.addRow("Stale threshold", self.stale_s)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0.1, 10.0)
        self.timeout.setDecimals(1)
        self.timeout.setValue(cfg.modbus_timeout_s)
        self.timeout.setSuffix(" s")
        mf.addRow("Modbus timeout", self.timeout)
        root.addWidget(mon)

        proto = QGroupBox("Protocol (verify live before trusting writes)")
        pf = QFormLayout(proto)
        self.word_order = QComboBox()
        self.word_order.addItems(list(WORD_ORDERS))
        self.word_order.setCurrentText(cfg.word_order)
        self.word_order.setToolTip("32-bit register-pair order within a "
                                   "Crimson L4 element — determined from "
                                   "a live nonzero value")
        pf.addRow("32-bit word order", self.word_order)
        self.wo_verified = QCheckBox("Word order verified against live "
                                     "data")
        self.wo_verified.setChecked(cfg.word_order_verified)
        pf.addRow("", self.wo_verified)
        self.rpm_scale = QDoubleSpinBox()
        self.rpm_scale.setRange(0.001, 1000.0)
        self.rpm_scale.setDecimals(3)
        self.rpm_scale.setValue(cfg.rpm_scale)
        self.rpm_scale.setToolTip("Engineering RPM per register count "
                                  "(Crimson may serve ×10)")
        pf.addRow("RPM scale", self.rpm_scale)
        root.addWidget(proto)

        br = QGroupBox("Bearing temperatures (extended gateway block)")
        bf = QFormLayout(br)
        self.bearing_temps = QCheckBox(
            "Read bearing temps B1/B2/B3 (Block1 elements 17–19)")
        self.bearing_temps.setChecked(cfg.bearing_temps)
        self.bearing_temps.setToolTip(
            "Requires the Crimson read gateway block to be extended with "
            "elements 17–19 mapped to Analog_Feedback.B1/B2/B3 and "
            "re-downloaded to the G315 first (see README). Applies at "
            "the next connect.")
        bf.addRow("", self.bearing_temps)
        self.bearing_unit = QLineEdit(cfg.bearing_unit)
        self.bearing_unit.setFixedWidth(60)
        self.bearing_unit.setToolTip(
            "Display unit of the scaled values — the tunnel_tags.csv cal "
            "span (0–150) looks like °C but the cal vintage/unit still "
            "needs confirming on the rig.")
        bf.addRow("Unit label", self.bearing_unit)
        root.addWidget(br)

        wr = QGroupBox("Writes (TunnelControl)")
        wf = QFormLayout(wr)
        self.rpm_max = QDoubleSpinBox()
        self.rpm_max.setRange(0, 100_000)
        self.rpm_max.setDecimals(0)
        self.rpm_max.setValue(cfg.rpm_max)
        self.rpm_max.setToolTip("HARD ceiling for RPM commands. "
                                "0 = not configured → all RPM writes "
                                "refuse and arming is blocked.")
        wf.addRow("RPM max (0 = writes refused)", self.rpm_max)
        self.hold_ms = QSpinBox()
        self.hold_ms.setRange(50, 2000)
        self.hold_ms.setValue(cfg.button_hold_ms)
        self.hold_ms.setSuffix(" ms")
        wf.addRow("Momentary button hold", self.hold_ms)
        self.mom_verified = QCheckBox("Momentary pulse verified vs "
                                      "physical HMI buttons")
        self.mom_verified.setChecked(cfg.momentary_verified)
        wf.addRow("", self.mom_verified)
        root.addWidget(wr)

        note = QLabel("Poll/stale changes apply on the next poll. "
                      "Word order and RPM scale apply at the next "
                      "connect.")
        note.setObjectName("dim")
        note.setWordWrap(True)
        root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def accept(self) -> None:
        cfg = self._cfg
        cfg.poll_s = self.poll_s.value()
        cfg.stale_after_s = self.stale_s.value()
        cfg.modbus_timeout_s = self.timeout.value()
        cfg.word_order = self.word_order.currentText()
        cfg.word_order_verified = self.wo_verified.isChecked()
        cfg.rpm_scale = self.rpm_scale.value()
        cfg.bearing_temps = self.bearing_temps.isChecked()
        cfg.bearing_unit = self.bearing_unit.text().strip() or "°C"
        cfg.rpm_max = self.rpm_max.value()
        cfg.button_hold_ms = self.hold_ms.value()
        cfg.momentary_verified = self.mom_verified.isChecked()
        super().accept()
