"""Filter Study tab — one channel viewed at several low-pass cutoffs
side by side, with live noise statistics per cutoff.

Built for the balance-noise work: pick a bridge channel, watch the raw
trace and a ladder of cutoffs (100/30/10/3/1 Hz) overlaid, and read the
rms / peak-to-peak noise each cutoff leaves behind. That directly
answers "what display cutoff do I want" and "how much of this noise is
broadband" (uncorrelated noise falls with the square root of bandwidth;
a mains or switching tone does NOT — it steps out when the cutoff
crosses its frequency).

Everything here is display-side only: the data of record is the
oversample-averaged ``scan_hz`` stream in the ring buffer, untouched.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from ni_usb_6351 import theme
from ni_usb_6351.config import ChannelConfig
from ni_usb_6351.datamodel import ScanRingBuffer

from .plots import _envelope, _style_plot, add_clear_action, lowpass

#: the cutoff ladder [Hz]; 0 = raw (no filter)
_CUTOFFS = (0.0, 100.0, 30.0, 10.0, 3.0, 1.0)


def _cutoff_label(hz: float) -> str:
    return "raw" if hz <= 0 else f"{hz:g} Hz"


class FilterPanel(QWidget):
    """Live filter-cutoff comparison for one channel."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._ring: Optional[ScanRingBuffer] = None
        self._chans: List[ChannelConfig] = []
        self._curves: Dict[float, pg.PlotDataItem] = {}
        # clear watermark: only samples newer than this are drawn
        self._clear_t = -np.inf
        self._last_t: Optional[float] = None

        root = QVBoxLayout(self)
        root.setSpacing(8)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Channel"))
        self.chan_combo = QComboBox()
        self.chan_combo.setMinimumWidth(120)
        bar.addWidget(self.chan_combo)
        bar.addSpacing(16)
        bar.addWidget(QLabel("Window"))
        self.window_spin = QDoubleSpinBox()
        self.window_spin.setRange(1.0, 120.0)
        self.window_spin.setDecimals(0)
        self.window_spin.setValue(10.0)
        self.window_spin.setSuffix(" s")
        bar.addWidget(self.window_spin)
        bar.addSpacing(16)
        self._checks: Dict[float, QCheckBox] = {}
        for hz in _CUTOFFS:
            cb = QCheckBox(_cutoff_label(hz))
            cb.setChecked(True)
            cb.toggled.connect(self._selection_changed)
            self._checks[hz] = cb
            bar.addWidget(cb)
        bar.addStretch(1)
        root.addLayout(bar)

        note = QLabel(
            "Display-side comparison only — the recorded stream is the "
            "oversample-averaged scan-rate data, unfiltered. Uncorrelated "
            "noise should drop ~√(cutoff); a tone (mains, switching "
            "supply) disappears in one step when the cutoff crosses it.")
        note.setWordWrap(True)
        note.setObjectName("dim")
        root.addWidget(note)

        self._plot = pg.PlotWidget()
        _style_plot(self._plot)
        pi = self._plot.getPlotItem()
        pi.setLabel("left", "input  (V)")
        pi.setLabel("bottom", "time before now  (s)")
        pi.addLegend(offset=(8, 8), labelTextColor=theme.TEXT,
                     brush=pg.mkBrush(theme.PLOT_BG + "cc"),
                     pen=pg.mkPen(theme.BORDER))
        add_clear_action(self._plot, self._clear_plot)
        root.addWidget(self._plot, 1)

        self.stats = QTableWidget(0, 3)
        self.stats.setHorizontalHeaderLabels(
            ["Cutoff", "σ noise (µV rms)", "peak-to-peak (µV)"])
        self.stats.verticalHeader().setVisible(False)
        self.stats.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.stats.horizontalHeader().setStretchLastSection(True)
        self.stats.setColumnWidth(0, 90)
        self.stats.setColumnWidth(1, 160)
        self.stats.setMaximumHeight(210)
        root.addWidget(self.stats)

        self._rebuild_curves()

    # ── wiring ───────────────────────────────────────────────────────────
    def set_channels(self, channels: List[ChannelConfig],
                     ring: Optional[ScanRingBuffer]) -> None:
        """Bind to the (re)started acquisition's channels + ring."""
        self._ring = ring
        self._chans = list(channels)
        current = self.chan_combo.currentText()
        self.chan_combo.blockSignals(True)
        self.chan_combo.clear()
        for ch in self._chans:
            self.chan_combo.addItem(ch.name)
        if current:
            idx = self.chan_combo.findText(current)
            if idx >= 0:
                self.chan_combo.setCurrentIndex(idx)
        self.chan_combo.blockSignals(False)

    def _selection_changed(self, *_a) -> None:
        self._rebuild_curves()

    def _clear_plot(self) -> None:
        if self._last_t is not None:
            self._clear_t = self._last_t

    def _rebuild_curves(self) -> None:
        pi = self._plot.getPlotItem()
        pi.clearPlots()
        self._curves = {}
        active = [hz for hz in _CUTOFFS if self._checks[hz].isChecked()]
        for i, hz in enumerate(active):
            # width-1 non-AA pens: same streaming-repaint constraint as
            # ChannelHistory
            pen = pg.mkPen(theme.series_color(i), width=1)
            self._curves[hz] = pi.plot([], [], name=_cutoff_label(hz),
                                       pen=pen, antialias=False)
        self.stats.setRowCount(len(active))
        for row, hz in enumerate(active):
            item = QTableWidgetItem(_cutoff_label(hz))
            item.setForeground(pg.mkColor(theme.series_color(row)))
            self.stats.setItem(row, 0, item)
            self.stats.setItem(row, 1, QTableWidgetItem("--"))
            self.stats.setItem(row, 2, QTableWidgetItem("--"))

    # ── refresh ──────────────────────────────────────────────────────────
    def refresh(self, rate_hz: float) -> None:
        if (self._ring is None or not self.isVisible()
                or not self._curves or rate_hz <= 0):
            return
        name = self.chan_combo.currentText()
        if not name:
            return
        window = float(self.window_spin.value())
        n = int(window * rate_hz * 1.05) + 2
        data = self._ring.tail(n, fields=["t", f"{name}_V"])
        t = data["t"]
        y_raw = data.get(f"{name}_V")
        if t.size < 2 or y_raw is None:
            return
        self._last_t = float(t[-1])
        x = t - t[-1]
        keep = (x >= -window) & (t > self._clear_t)
        x, y_raw = x[keep], y_raw[keep]
        for row, (hz, curve) in enumerate(self._curves.items()):
            y = lowpass(y_raw, rate_hz, hz)
            xd, yd = _envelope(x, y)
            curve.setData(xd, yd)
            # stats on the central region — the moving-average kernel
            # smears edge samples, which would corrupt σ/p-p
            trim = int(rate_hz / hz) if hz > 0 else 0
            core = y[trim:y.size - trim] if y.size > 3 * max(trim, 1) else y
            if core.size >= 2:
                sigma = float(np.std(core)) * 1e6
                ptp = float(np.ptp(core)) * 1e6
                self._set_stat(row, 1, f"{sigma:,.2f}")
                self._set_stat(row, 2, f"{ptp:,.2f}")

    def _set_stat(self, row: int, col: int, text: str) -> None:
        item = self.stats.item(row, col)
        if item is not None and item.text() != text:
            item.setText(text)
