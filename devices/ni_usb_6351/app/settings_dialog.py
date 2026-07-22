"""Settings dialog — deeper NiDaqConfig options (File → Settings…)."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QGroupBox,
    QLineEdit, QSpinBox, QVBoxLayout,
)

from ni_usb_6351.config import NiDaqConfig


class SettingsDialog(QDialog):
    def __init__(self, cfg: NiDaqConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)
        self._cfg = cfg
        self._build(cfg)

    def _build(self, cfg: NiDaqConfig):
        root = QVBoxLayout(self)

        dev = QGroupBox("Device (applies at next Connect)")
        df = QFormLayout(dev)
        self.device_name = QLineEdit(cfg.device_name)
        self.device_name.setToolTip("NI-MAX device alias (e.g. Dev2)")
        df.addRow("Device name", self.device_name)
        root.addWidget(dev)

        acq = QGroupBox("Acquisition (applies at next Connect)")
        af = QFormLayout(acq)
        self.scan_hz = QDoubleSpinBox()
        self.scan_hz.setRange(1.0, 1_250_000.0)
        self.scan_hz.setDecimals(0)
        self.scan_hz.setValue(cfg.scan_hz)
        self.scan_hz.setSuffix(" Hz")
        af.addRow("Scan rate", self.scan_hz)
        self.buffer_s = QDoubleSpinBox()
        self.buffer_s.setRange(1.0, 60.0)
        self.buffer_s.setDecimals(0)
        self.buffer_s.setValue(cfg.buffer_seconds)
        self.buffer_s.setSuffix(" s")
        af.addRow("Driver buffer", self.buffer_s)
        self.poll_ms = QSpinBox()
        self.poll_ms.setRange(5, 500)
        self.poll_ms.setValue(cfg.poll_ms)
        self.poll_ms.setSuffix(" ms")
        af.addRow("Transfer poll period", self.poll_ms)
        self.ao_update_hz = QDoubleSpinBox()
        self.ao_update_hz.setRange(100.0, 2_000_000.0)
        self.ao_update_hz.setDecimals(0)
        self.ao_update_hz.setValue(cfg.ao_update_hz)
        self.ao_update_hz.setSuffix(" Hz")
        af.addRow("AO waveform clock", self.ao_update_hz)
        root.addWidget(acq)

        disp = QGroupBox("Display")
        pf = QFormLayout(disp)
        self.plot_window = QDoubleSpinBox()
        self.plot_window.setRange(1.0, 600.0)
        self.plot_window.setDecimals(0)
        self.plot_window.setValue(cfg.plot_window_s)
        self.plot_window.setSuffix(" s")
        pf.addRow("Time-history window", self.plot_window)
        self.tile_avg = QSpinBox()
        self.tile_avg.setRange(10, 5000)
        self.tile_avg.setValue(cfg.tile_avg_ms)
        self.tile_avg.setSuffix(" ms")
        pf.addRow("Tile smoothing", self.tile_avg)
        root.addWidget(disp)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def accept(self) -> None:
        cfg = self._cfg
        cfg.device_name = self.device_name.text().strip() or cfg.device_name
        cfg.scan_hz = float(self.scan_hz.value())
        cfg.buffer_seconds = float(self.buffer_s.value())
        cfg.poll_ms = self.poll_ms.value()
        cfg.ao_update_hz = float(self.ao_update_hz.value())
        cfg.plot_window_s = float(self.plot_window.value())
        cfg.tile_avg_ms = self.tile_avg.value()
        super().accept()
