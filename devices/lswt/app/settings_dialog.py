"""Settings dialog — General (comms/poll) and Advanced (control) tabs."""

from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QLabel, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from lswt.config import LswtConfig

log = logging.getLogger(__name__)


class SettingsDialog(QDialog):
    def __init__(self, cfg: LswtConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Settings — {cfg.label}")
        self.setMinimumWidth(520)
        self._cfg = cfg
        self._build(cfg)

    def _build(self, cfg: LswtConfig):
        root = QVBoxLayout(self)
        tabs = QTabWidget()
        general = QWidget()
        gen = QVBoxLayout(general)
        advanced = QWidget()
        adv = QVBoxLayout(advanced)
        tabs.addTab(general, "General")
        tabs.addTab(advanced, "Advanced")
        root.addWidget(tabs)

        # ── General: comms / poll ──
        comms = QGroupBox("Modbus TCP (ABB ACS530)")
        cf = QFormLayout(comms)
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(cfg.port)
        cf.addRow("Port", self.port)
        self.unit_id = QSpinBox()
        self.unit_id.setRange(0, 255)
        self.unit_id.setValue(cfg.unit_id)
        self.unit_id.setToolTip("Modbus unit/slave id — the deployed C# "
                                "used 1")
        cf.addRow("Unit id", self.unit_id)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0.1, 10.0)
        self.timeout.setDecimals(1)
        self.timeout.setValue(cfg.modbus_timeout_s)
        self.timeout.setSuffix(" s")
        cf.addRow("Modbus timeout", self.timeout)
        gen.addWidget(comms)

        mon = QGroupBox("Monitoring")
        mf = QFormLayout(mon)
        self.poll = QDoubleSpinBox()
        self.poll.setRange(0.05, 5.0)
        self.poll.setDecimals(2)
        self.poll.setSingleStep(0.05)
        self.poll.setValue(cfg.poll_s)
        self.poll.setSuffix(" s")
        self.poll.setToolTip("Actual-Hz poll / ramp tick period")
        mf.addRow("Poll period", self.poll)
        self.stale = QDoubleSpinBox()
        self.stale.setRange(0.5, 60.0)
        self.stale.setDecimals(1)
        self.stale.setValue(cfg.stale_after_s)
        self.stale.setSuffix(" s")
        self.stale.setToolTip(
            "No successful poll within this window → status STALE. "
            "Comm loss is an ALERT only — the fan is deliberately NOT "
            "auto-stopped (the drive holds its reference safely; "
            "auto-stop would turn a network blip into an aborted run).")
        mf.addRow("Stale after", self.stale)
        self.window = QDoubleSpinBox()
        self.window.setRange(10.0, 3600.0)
        self.window.setDecimals(0)
        self.window.setValue(cfg.plot_window_s)
        self.window.setSuffix(" s")
        mf.addRow("Plot window", self.window)
        gen.addWidget(mon)
        gen.addStretch(1)

        # ── Advanced: control ──
        ctl = QGroupBox("Fan control")
        af = QFormLayout(ctl)
        self.max_hz = QDoubleSpinBox()
        self.max_hz.setRange(1.0, 60.0)
        self.max_hz.setDecimals(1)
        self.max_hz.setValue(cfg.max_hz)
        self.max_hz.setSuffix(" Hz")
        self.max_hz.setToolTip("Setpoint/reference clamp. The ACS530 "
                               "full scale is 60 Hz (reference 20000).")
        af.addRow("Max frequency", self.max_hz)
        self.ramp = QDoubleSpinBox()
        self.ramp.setRange(0.1, 30.0)
        self.ramp.setDecimals(1)
        self.ramp.setSingleStep(0.5)
        self.ramp.setValue(cfg.ramp_hz_per_s)
        self.ramp.setSuffix(" Hz/s")
        self.ramp.setToolTip(
            "Host-side setpoint ramp — the commanded reference moves "
            "toward the setpoint at this rate and never step-jumps the "
            "fan. (Replaces the old C# tool's '>2 ft/s change → "
            "command 0' lockout.)")
        af.addRow("Ramp rate", self.ramp)
        self.ref_sign = QComboBox()
        self.ref_sign.addItems(["−1  (negative — C# convention)",
                                "+1  (positive)"])
        self.ref_sign.setCurrentIndex(0 if cfg.reference_sign < 0 else 1)
        self.ref_sign.setToolTip(
            "Sign of the written speed reference. The deployed C# wrote "
            "the NEGATIVE of the scaled value — a direction convention "
            "on these fans. VERIFY on the first live run at a tiny "
            "reference: a wrong sign would command the fan in REVERSE.")
        af.addRow("Reference sign", self.ref_sign)
        adv.addWidget(ctl)

        note = QLabel(
            "Sign/comms changes apply at the next Connect; ramp and "
            "max Hz apply immediately to new setpoints.")
        note.setObjectName("dim")
        note.setWordWrap(True)
        adv.addWidget(note)
        adv.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def accept(self) -> None:
        cfg = self._cfg
        cfg.port = self.port.value()
        cfg.unit_id = self.unit_id.value()
        cfg.modbus_timeout_s = self.timeout.value()
        cfg.poll_s = self.poll.value()
        cfg.stale_after_s = self.stale.value()
        cfg.plot_window_s = self.window.value()
        cfg.max_hz = self.max_hz.value()
        cfg.ramp_hz_per_s = self.ramp.value()
        cfg.reference_sign = -1 if self.ref_sign.currentIndex() == 0 else 1
        super().accept()
