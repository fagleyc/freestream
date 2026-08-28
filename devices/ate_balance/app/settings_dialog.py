"""Settings dialog — the deeper AteConfig options that don't fit the
connection bar.  Opened from File → Settings…; writes back into the live
config on OK (network items take effect at the next Connect).
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFormLayout, QGroupBox, QLineEdit, QSpinBox, QVBoxLayout,
)

from ate_balance.config import (AteConfig, LOAD_UNITS,
                                LOAD_UNIT_SYMBOLS)


class SettingsDialog(QDialog):
    def __init__(self, cfg: AteConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(420)
        self._cfg = cfg
        self._build(cfg)

    def _build(self, cfg: AteConfig):
        root = QVBoxLayout(self)

        # ── Network ──
        net = QGroupBox("Network (applies at next Connect)")
        nf = QFormLayout(net)
        self.bind_host = QLineEdit(cfg.bind_host)
        self.bind_host.setToolTip(
            "Local interface to bind the TMSC/TMSD listeners on.\n"
            "0.0.0.0 = all interfaces; set to this PC's rig-side IP\n"
            "(e.g. 192.168.1.101) to pin the link to the rig network.")
        nf.addRow("Bind host", self.bind_host)
        self.auto_trigger = QCheckBox("Send TMS_CONNECT automatically on Connect")
        self.auto_trigger.setChecked(cfg.auto_trigger)
        nf.addRow(self.auto_trigger)
        root.addWidget(net)

        # ── Sampling ──
        samp = QGroupBox("Sampling")
        sf = QFormLayout(samp)
        self.sample_secs = QSpinBox()
        self.sample_secs.setRange(1, 300)
        self.sample_secs.setValue(cfg.default_sample_seconds)
        self.sample_secs.setSuffix(" s")
        self.sample_secs.setToolTip("Default duration for TAKE_SAMPLE requests")
        sf.addRow("Default sample duration", self.sample_secs)
        root.addWidget(samp)

        # ── Display ──
        disp = QGroupBox("Display")
        df = QFormLayout(disp)
        self.plot_window = QDoubleSpinBox()
        self.plot_window.setRange(1.0, 120.0)
        self.plot_window.setDecimals(0)
        self.plot_window.setValue(cfg.plot_window_s)
        self.plot_window.setSuffix(" s")
        self.plot_window.setToolTip("Default time-history window length")
        df.addRow("Time-history window", self.plot_window)
        self.bar_avg = QSpinBox()
        self.bar_avg.setRange(10, 2000)
        self.bar_avg.setValue(cfg.bar_avg_ms)
        self.bar_avg.setSuffix(" ms")
        self.bar_avg.setToolTip(
            "Averaging window for the live bars (10 ms = raw & twitchy,\n"
            "200 ms = calm). Time histories always show the raw stream.")
        df.addRow("Live bar smoothing", self.bar_avg)
        self.lpf = QDoubleSpinBox()
        self.lpf.setRange(0.0, 100.0)
        self.lpf.setDecimals(2)
        self.lpf.setSingleStep(0.5)
        self.lpf.setValue(getattr(cfg, "display_lpf_hz", 1.0))
        self.lpf.setSuffix(" Hz")
        self.lpf.setToolTip(
            "Low-pass cutoff for the LIVE bars and traces (0 = off).\n"
            "Display only — the ring buffer, the dwell averages and\n"
            "everything recorded stay raw.")
        df.addRow("Display low-pass", self.lpf)
        self.units = QComboBox()
        for key in LOAD_UNITS:
            f_u, m_u = LOAD_UNIT_SYMBOLS[key]
            self.units.addItem(f"{f_u} / {m_u}", key)
        idx = self.units.findData(getattr(cfg, "load_units", "lb"))
        self.units.setCurrentIndex(max(idx, 0))
        self.units.setToolTip(
            "MUST match the OGI's Settings → Units selection.\n"
            "The loads arrive as bare numbers with no unit tag, so a\n"
            "mismatch is a silent scale error downstream (N read as lb\n"
            "is 4.45x; lbf·ft read as in·lbf is 12x).")
        df.addRow("Load units (match the OGI)", self.units)
        root.addWidget(disp)

        # ── Rated load maxima ──
        lim = QGroupBox("Rated load maxima  (N / N·m; 0 = no limit)")
        lf = QFormLayout(lim)
        self.max_spins = {}
        for axis, unit in (("Fx", " N"), ("Fy", " N"), ("Fz", " N"),
                           ("Mx", " N·m"), ("My", " N·m"),
                           ("Mz", " N·m")):
            sp = self._dbl(cfg.max_loads.get(axis, 0.0), 0.0, 100000.0,
                           1, unit)
            sp.setToolTip(
                f"Rated maximum for the {axis} channel, in N / N·m\n"
                "REGARDLESS of what the OGI is streaming — the bars\n"
                "convert, so changing the unit setting cannot silently\n"
                "invalidate these. Seeded from the balance's published\n"
                "design ranges (AID-010-10015-1 §2.4). 0 = no limit.")
            lf.addRow(f"{axis} max", sp)
            self.max_spins[axis] = sp
        root.addWidget(lim)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _dbl(value: float, lo: float, hi: float, decimals: int,
             suffix: str) -> QDoubleSpinBox:
        sp = QDoubleSpinBox()
        sp.setRange(lo, hi)
        sp.setDecimals(decimals)
        sp.setValue(value)
        sp.setSuffix(suffix)
        return sp

    def accept(self) -> None:
        cfg = self._cfg
        cfg.bind_host = self.bind_host.text().strip() or "0.0.0.0"
        cfg.auto_trigger = self.auto_trigger.isChecked()
        cfg.default_sample_seconds = self.sample_secs.value()
        cfg.plot_window_s = float(self.plot_window.value())
        cfg.bar_avg_ms = self.bar_avg.value()
        cfg.display_lpf_hz = float(self.lpf.value())
        cfg.load_units = self.units.currentData()
        cfg.max_loads = {a: sp.value() for a, sp in self.max_spins.items()}
        super().accept()
