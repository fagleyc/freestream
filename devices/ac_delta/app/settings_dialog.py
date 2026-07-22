"""Settings dialog — loop tuning, speed bands, limits (File → Settings…)."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QSpinBox, QVBoxLayout,
)

from ac_delta.config import CrescentConfig


class SettingsDialog(QDialog):
    def __init__(self, cfg: CrescentConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        self._cfg = cfg
        self._build(cfg)

    def _build(self, cfg: CrescentConfig):
        root = QVBoxLayout(self)

        loop = QGroupBox("Position loop")
        lf = QFormLayout(loop)
        self.loop_ms = QSpinBox()
        self.loop_ms.setRange(20, 1000)
        self.loop_ms.setValue(cfg.loop_ms)
        self.loop_ms.setSuffix(" ms")
        self.loop_ms.setToolTip("Loop period. The legacy C# ran 100 ms; "
                                "50 ms brakes more precisely.")
        lf.addRow("Loop period", self.loop_ms)

        bands_row = QHBoxLayout()
        self.bands = []
        for i, v in enumerate(cfg.speed_bands_deg):
            sp = QDoubleSpinBox()
            sp.setRange(0.05, 45.0)
            sp.setDecimals(2)
            sp.setValue(v)
            sp.setSuffix("°")
            sp.setToolTip(f"Below this remaining distance → speed step "
                          f"{i + 1}")
            self.bands.append(sp)
            bands_row.addWidget(sp)
        lf.addRow("Decel bands (steps 1–4)", bands_row)

        self.max_step = QSpinBox()
        self.max_step.setRange(1, 5)
        self.max_step.setValue(cfg.max_step)
        self.max_step.setToolTip("Cap the top speed step (5 = full speed)")
        lf.addRow("Max speed step", self.max_step)
        root.addWidget(loop)

        limits = QGroupBox("Soft travel limits (deg)")
        lg = QFormLayout(limits)
        self.a_min, self.a_max = self._pair(cfg.alpha.min_deg,
                                            cfg.alpha.max_deg)
        lg.addRow("Alpha min / max", self._row(self.a_min, self.a_max))
        self.b_min, self.b_max = self._pair(cfg.beta.min_deg,
                                            cfg.beta.max_deg)
        lg.addRow("Beta min / max", self._row(self.b_min, self.b_max))

        self.a_tol = QDoubleSpinBox()
        self.a_tol.setRange(0.005, 2.0)
        self.a_tol.setDecimals(3)
        self.a_tol.setValue(cfg.alpha.tolerance_deg)
        self.a_tol.setSuffix("°")
        lg.addRow("Alpha move tolerance", self.a_tol)
        self.b_tol = QDoubleSpinBox()
        self.b_tol.setRange(0.005, 2.0)
        self.b_tol.setDecimals(3)
        self.b_tol.setValue(cfg.beta.tolerance_deg)
        self.b_tol.setSuffix("°")
        lg.addRow("Beta move tolerance", self.b_tol)
        root.addWidget(limits)

        mb = QGroupBox("Modbus")
        mf = QFormLayout(mb)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0.1, 10.0)
        self.timeout.setDecimals(1)
        self.timeout.setValue(cfg.modbus_timeout_s)
        self.timeout.setSuffix(" s")
        mf.addRow("Timeout", self.timeout)
        self.max_err = QSpinBox()
        self.max_err.setRange(1, 50)
        self.max_err.setValue(cfg.max_consecutive_errors)
        self.max_err.setToolTip("Watchdog: consecutive comm failures before "
                                "stopping all axes")
        mf.addRow("Watchdog error count", self.max_err)
        root.addWidget(mb)

        note = QLabel("Loop/band changes apply immediately at the next "
                      "tick; limits apply to new move commands.")
        note.setObjectName("dim")
        root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _pair(lo, hi):
        a = QDoubleSpinBox()
        a.setRange(-360, 360)
        a.setDecimals(1)
        a.setValue(lo)
        a.setSuffix("°")
        b = QDoubleSpinBox()
        b.setRange(-360, 360)
        b.setDecimals(1)
        b.setValue(hi)
        b.setSuffix("°")
        return a, b

    @staticmethod
    def _row(*widgets):
        row = QHBoxLayout()
        for w in widgets:
            row.addWidget(w)
        return row

    def accept(self) -> None:
        cfg = self._cfg
        cfg.loop_ms = self.loop_ms.value()
        cfg.speed_bands_deg = sorted(sp.value() for sp in self.bands)
        cfg.max_step = self.max_step.value()
        cfg.alpha.min_deg = min(self.a_min.value(), self.a_max.value())
        cfg.alpha.max_deg = max(self.a_min.value(), self.a_max.value())
        cfg.beta.min_deg = min(self.b_min.value(), self.b_max.value())
        cfg.beta.max_deg = max(self.b_min.value(), self.b_max.value())
        cfg.alpha.tolerance_deg = self.a_tol.value()
        cfg.beta.tolerance_deg = self.b_tol.value()
        cfg.modbus_timeout_s = self.timeout.value()
        cfg.max_consecutive_errors = self.max_err.value()
        super().accept()
