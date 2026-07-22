"""Settings dialog — serial, motion parameters, behaviour (File →
Settings…)."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QLabel, QLineEdit, QSpinBox, QVBoxLayout,
)

from lswt_sting.config import StingConfig


class SettingsDialog(QDialog):
    def __init__(self, cfg: StingConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        self._cfg = cfg
        self._build(cfg)

    def _build(self, cfg: StingConfig):
        root = QVBoxLayout(self)

        ser = QGroupBox("Serial")
        sf = QFormLayout(ser)
        self.com_port = QLineEdit(cfg.com_port)
        self.com_port.setToolTip("RS-232 port for the drive daisy chain "
                                 "(9600-8N1, fixed)")
        sf.addRow("COM port", self.com_port)
        self.poll_ms = QSpinBox()
        self.poll_ms.setRange(20, 2000)
        self.poll_ms.setValue(cfg.poll_ms)
        self.poll_ms.setSuffix(" ms")
        self.poll_ms.setToolTip("Status/position poll period")
        sf.addRow("Poll period", self.poll_ms)
        self.init_reset = QCheckBox("Send Z (drive reset) during connect "
                                    "init (legacy InitHw)")
        self.init_reset.setChecked(cfg.init_reset)
        sf.addRow(self.init_reset)
        root.addWidget(ser)

        mot = QGroupBox("Motion parameters (drive units; applied at "
                        "next connect)")
        mf = QFormLayout(mot)
        self.a_acc, self.a_dec, self.a_vel = self._axis_rows(
            mf, "Alpha", cfg.alpha)
        self.b_acc, self.b_dec, self.b_vel = self._axis_rows(
            mf, "Beta", cfg.beta)
        root.addWidget(mot)

        beh = QGroupBox("Behaviour")
        bf = QFormLayout(beh)
        self.park = QCheckBox("Park Alpha on disconnect (legacy "
                              "'off position')")
        self.park.setChecked(cfg.park_on_disconnect)
        bf.addRow(self.park)
        self.park_deg = QDoubleSpinBox()
        self.park_deg.setRange(-360.0, 360.0)
        self.park_deg.setDecimals(1)
        self.park_deg.setValue(cfg.park_alpha_deg)
        self.park_deg.setSuffix("°")
        bf.addRow("Park Alpha at", self.park_deg)
        self.plot_window = QDoubleSpinBox()
        self.plot_window.setRange(5.0, 3600.0)
        self.plot_window.setDecimals(0)
        self.plot_window.setValue(cfg.plot_window_s)
        self.plot_window.setSuffix(" s")
        bf.addRow("History window", self.plot_window)
        root.addWidget(beh)

        note = QLabel("Serial and motion-parameter changes apply at the "
                      "next connect.")
        note.setObjectName("dim")
        root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _axis_rows(form: QFormLayout, name: str, ax_cfg):
        acc = QLineEdit(ax_cfg.acceleration)
        dec = QLineEdit(ax_cfg.deceleration)
        vel = QLineEdit(ax_cfg.velocity)
        for w, tip in ((acc, "A command"), (dec, "AD command"),
                       (vel, "V command")):
            w.setToolTip(f"Sent verbatim as the {tip} (rev/s, rev/s²)")
        form.addRow(f"{name} acceleration", acc)
        form.addRow(f"{name} deceleration", dec)
        form.addRow(f"{name} velocity", vel)
        spd = QLabel(f"{ax_cfg.steps_per_degree:.6f}")
        spd.setObjectName("dim")
        spd.setToolTip("Fixed constant from the deployed legacy tool")
        form.addRow(f"{name} steps/degree", spd)
        return acc, dec, vel

    def accept(self) -> None:
        cfg = self._cfg
        cfg.com_port = self.com_port.text().strip() or cfg.com_port
        cfg.poll_ms = self.poll_ms.value()
        cfg.init_reset = self.init_reset.isChecked()
        for ax_cfg, acc, dec, vel in (
                (cfg.alpha, self.a_acc, self.a_dec, self.a_vel),
                (cfg.beta, self.b_acc, self.b_dec, self.b_vel)):
            ax_cfg.acceleration = acc.text().strip() or ax_cfg.acceleration
            ax_cfg.deceleration = dec.text().strip() or ax_cfg.deceleration
            ax_cfg.velocity = vel.text().strip() or ax_cfg.velocity
        cfg.park_on_disconnect = self.park.isChecked()
        cfg.park_alpha_deg = self.park_deg.value()
        cfg.plot_window_s = self.plot_window.value()
        super().accept()
