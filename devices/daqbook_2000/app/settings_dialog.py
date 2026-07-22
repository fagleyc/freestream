"""Settings dialog — deeper DaqbookConfig options (File → Settings…)."""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLineEdit, QPushButton, QSpinBox, QVBoxLayout,
)

from daqbook_2000.config import DaqbookConfig


class SettingsDialog(QDialog):
    def __init__(self, cfg: DaqbookConfig, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(480)
        self._cfg = cfg
        self._build(cfg)

    def _build(self, cfg: DaqbookConfig):
        root = QVBoxLayout(self)

        dev = QGroupBox("Device (applies at next Connect)")
        df = QFormLayout(dev)
        self.device_name = QLineEdit(cfg.device_name)
        self.device_name.setToolTip(
            "Alias configured in the Daq Configuration applet")
        df.addRow("Device alias", self.device_name)
        self.device_ip = QLineEdit(cfg.device_ip)
        self.device_ip.setToolTip(
            "Informational — the applet owns the alias→IP mapping")
        df.addRow("Device IP (info)", self.device_ip)

        dll_row = QHBoxLayout()
        self.dll_path = QLineEdit(cfg.dll_path)
        self.dll_path.setPlaceholderText("(search standard locations)")
        dll_row.addWidget(self.dll_path, 1)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_dll)
        dll_row.addWidget(browse)
        df.addRow("DaqX64.dll path", dll_row)
        root.addWidget(dev)

        acq = QGroupBox("Acquisition (applies at next Connect)")
        af = QFormLayout(acq)
        self.scan_hz = QDoubleSpinBox()
        self.scan_hz.setRange(1.0, 100_000.0)
        self.scan_hz.setDecimals(0)
        self.scan_hz.setValue(cfg.scan_hz)
        self.scan_hz.setSuffix(" Hz")
        af.addRow("Scan rate", self.scan_hz)
        self.buffer_s = QDoubleSpinBox()
        self.buffer_s.setRange(1.0, 60.0)
        self.buffer_s.setDecimals(0)
        self.buffer_s.setValue(cfg.buffer_seconds)
        self.buffer_s.setSuffix(" s")
        self.buffer_s.setToolTip("Circular driver-buffer length")
        af.addRow("Driver buffer", self.buffer_s)
        self.poll_ms = QSpinBox()
        self.poll_ms.setRange(5, 500)
        self.poll_ms.setValue(cfg.poll_ms)
        self.poll_ms.setSuffix(" ms")
        af.addRow("Transfer poll period", self.poll_ms)
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

    def _browse_dll(self):
        path, _ = QFileDialog.getOpenFileName(self, "Locate DaqX64.dll", "",
                                              "DLL (*.dll)")
        if path:
            self.dll_path.setText(path)

    def accept(self) -> None:
        cfg = self._cfg
        cfg.device_name = self.device_name.text().strip() or cfg.device_name
        cfg.device_ip = self.device_ip.text().strip()
        cfg.dll_path = self.dll_path.text().strip()
        cfg.scan_hz = float(self.scan_hz.value())
        cfg.buffer_seconds = float(self.buffer_s.value())
        cfg.poll_ms = self.poll_ms.value()
        cfg.plot_window_s = float(self.plot_window.value())
        cfg.tile_avg_ms = self.tile_avg.value()
        super().accept()
