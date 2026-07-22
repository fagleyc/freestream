"""Connection panel — endpoints, role, sim toggle, connect/disconnect/trigger."""

from __future__ import annotations

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSpinBox,
)

from ate_balance import theme
from ate_balance.config import AteConfig, CONNECT_DIAL, CONNECT_LISTEN


class ConnectPanel(QGroupBox):
    connectRequested = pyqtSignal()
    disconnectRequested = pyqtSignal()
    triggerRequested = pyqtSignal()

    def __init__(self, cfg: AteConfig, parent=None):
        super().__init__("Connection", parent)
        self._build(cfg)

    def _build(self, cfg: AteConfig):
        row = QHBoxLayout(self)
        row.setSpacing(8)

        row.addWidget(QLabel("OGI IP"))
        self.ip_edit = QLineEdit(cfg.ogi_ip)
        self.ip_edit.setFixedWidth(120)
        row.addWidget(self.ip_edit)

        self.tmsc = self._port("TMSC", cfg.tmsc_port, row)
        self.tmsd = self._port("TMSD", cfg.tmsd_port, row)
        self.ogit = self._port("OGIT", cfg.ogit_port, row)

        row.addWidget(QLabel("Role"))
        self.mode = QComboBox()
        self.mode.addItems([CONNECT_LISTEN, CONNECT_DIAL])
        self.mode.setCurrentText(cfg.connect_mode)
        row.addWidget(self.mode)

        self.sim = QCheckBox("Simulate")
        self.sim.setChecked(cfg.force_sim)
        row.addWidget(self.sim)

        row.addStretch(1)

        self.lamp = QLabel("DISCONNECTED")
        self.lamp.setProperty("mono", "true")
        self.lamp.setStyleSheet(f"color: {theme.TEXT_DIM}; font-weight: bold;")
        row.addWidget(self.lamp)

        self.trigger_btn = QPushButton("Trigger")
        self.trigger_btn.setToolTip("Send TMS_CONNECT so the OGI dials this client")
        self.trigger_btn.clicked.connect(self.triggerRequested)
        row.addWidget(self.trigger_btn)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.clicked.connect(self.connectRequested)
        row.addWidget(self.connect_btn)

        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self.disconnectRequested)
        self.disconnect_btn.setEnabled(False)
        row.addWidget(self.disconnect_btn)

    def _port(self, label: str, value: int, row) -> QSpinBox:
        row.addWidget(QLabel(label))
        sp = QSpinBox()
        sp.setRange(1, 65535)
        sp.setValue(value)
        sp.setFixedWidth(72)
        row.addWidget(sp)
        return sp

    # ── read user entries back into a config ──
    def apply_to_config(self, cfg: AteConfig) -> None:
        cfg.ogi_ip = self.ip_edit.text().strip() or cfg.ogi_ip
        cfg.tmsc_port = self.tmsc.value()
        cfg.tmsd_port = self.tmsd.value()
        cfg.ogit_port = self.ogit.value()
        cfg.connect_mode = self.mode.currentText()
        cfg.force_sim = self.sim.isChecked()

    # ── state-driven UI ──
    def set_state(self, connected: bool, sim_mode: bool, link_up: bool) -> None:
        self.connect_btn.setEnabled(not connected)
        self.disconnect_btn.setEnabled(connected)
        for w in (self.ip_edit, self.tmsc, self.tmsd, self.ogit, self.mode, self.sim):
            w.setEnabled(not connected)
        self.trigger_btn.setEnabled(connected and not sim_mode)

        if not connected:
            self._set_lamp("DISCONNECTED", theme.TEXT_DIM)
        elif sim_mode:
            self._set_lamp("SIMULATION", theme.WARNING)
        elif link_up:
            self._set_lamp("LINKED", theme.SUCCESS)
        else:
            self._set_lamp("WAITING FOR OGI", theme.ACCENT_LIGHT)

    def _set_lamp(self, text: str, color: str) -> None:
        self.lamp.setText(text)
        self.lamp.setStyleSheet(f"color: {color}; font-weight: bold;")
