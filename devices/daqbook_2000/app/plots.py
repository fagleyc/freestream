"""pyqtgraph widgets for the DaqBook GUI.

* :class:`ChannelTiles`  - one stat tile per channel: big engineering value,
  raw volts beneath, channel color chip for identity.
* :class:`ChannelHistory` - stacked, x-linked scrolling plots (one per
  channel, since each channel has its own unit) drawn from the device ring
  buffer at the full scan rate.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from daqbook_2000 import theme
from daqbook_2000.config import ChannelConfig
from daqbook_2000.datamodel import ScanRingBuffer

theme.apply_pyqtgraph_theme()

_GRID_ALPHA = 0.25

# Cap on points handed to each curve per redraw. Data beyond this is
# min/max-binned first, so transients stay visible but Qt never has to
# draw tens of thousands of segments per tick (the source of GUI lag at
# high scan rates / long windows).
_MAX_PLOT_BINS = 1200


def _envelope(x: np.ndarray, y: np.ndarray, max_bins: int = _MAX_PLOT_BINS):
    """Min/max-decimate (x, y) to at most 2*max_bins points."""
    n = x.size
    if n <= max_bins * 2:
        return x, y
    stride = n // max_bins
    m = (n // stride) * stride
    yb = y[:m].reshape(-1, stride)
    xs = np.repeat(x[:m:stride], 2)
    ys = np.empty(xs.size, dtype=y.dtype)
    ys[0::2] = yb.min(axis=1)
    ys[1::2] = yb.max(axis=1)
    return xs, ys


class _Tile(QFrame):
    """One channel stat tile."""

    def __init__(self, name: str, unit: str, color: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"QFrame {{ background-color: {theme.BG_LIGHT}; "
            f"border: 1px solid {theme.BORDER}; border-radius: 6px; }}"
            f"QLabel {{ border: none; background: transparent; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(2)

        head = QHBoxLayout()
        chip = QLabel()
        chip.setFixedSize(10, 10)
        chip.setStyleSheet(f"background-color: {color}; border-radius: 5px;")
        head.addWidget(chip)
        title = QLabel(name)
        title.setStyleSheet(f"color: {theme.TEXT_DIM}; font-weight: bold;")
        head.addWidget(title)
        head.addStretch(1)
        lay.addLayout(head)

        self.value = QLabel("--")
        self.value.setStyleSheet(
            f"font-family: 'Segoe UI', sans-serif; font-size: 24pt; "
            f"font-weight: 600; color: {theme.TEXT};")
        lay.addWidget(self.value)

        self.sub = QLabel(unit)
        self.sub.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(self.sub)
        self._unit = unit

    def update_value(self, eng: float, volts: float) -> None:
        mag = abs(eng)
        decimals = 4 if mag < 1 else (3 if mag < 10 else 2)
        self.value.setText(f"{eng:,.{decimals}f}")
        self.sub.setText(f"{self._unit}   ({volts:+.4f} V)")


class ChannelTiles(QWidget):
    """Row of stat tiles, one per enabled channel, smoothed over avg_ms."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.avg_ms = 200
        self._tiles: Dict[str, _Tile] = {}
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(8)

    def set_channels(self, channels: List[ChannelConfig]) -> None:
        while self._lay.count():
            item = self._lay.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._tiles = {}
        for i, ch in enumerate(channels):
            tile = _Tile(ch.name, ch.unit, theme.series_color(i))
            self._tiles[ch.name] = tile
            self._lay.addWidget(tile)
        self._lay.addStretch(1)

    def refresh(self, ring: Optional[ScanRingBuffer], rate_hz: float) -> None:
        if ring is None or not self._tiles:
            return
        n = max(2, int(self.avg_ms / 1000.0 * max(rate_hz, 50.0)))
        data = ring.tail(n)
        if data["t"].size == 0:
            return
        for name, tile in self._tiles.items():
            if name in data:
                tile.update_value(float(np.mean(data[name])),
                                  float(np.mean(data[f"{name}_V"])))


class ChannelHistory(QWidget):
    """Stacked scrolling plots, one per channel, full scan rate."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.window_s = 30.0
        self.paused = False
        self.show_volts = False
        self._rate = 1000.0
        self._ring: Optional[ScanRingBuffer] = None
        self._curves: Dict[str, pg.PlotDataItem] = {}
        self._plots: List[pg.PlotWidget] = []
        self._units: Dict[str, str] = {}
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(4)

    def set_channels(self, channels: List[ChannelConfig],
                     ring: Optional[ScanRingBuffer]) -> None:
        self._ring = ring
        for pw in self._plots:
            pw.deleteLater()
        self._plots = []
        self._curves = {}
        self._units = {c.name: c.unit for c in channels}
        for i, ch in enumerate(channels):
            pw = pg.PlotWidget()
            pi = pw.getPlotItem()
            pi.showGrid(x=False, y=True, alpha=_GRID_ALPHA)
            pi.setMenuEnabled(False)
            pi.setMouseEnabled(x=False, y=True)
            pi.setClipToView(True)   # decimation happens in refresh()
            for side in ("left", "bottom"):
                ax = pi.getAxis(side)
                ax.setPen(pg.mkPen(theme.AXIS, width=1))
                ax.setTextPen(theme.TEXT_DIM)
                ax.enableAutoSIPrefix(False)   # no "(x0.001)" axis scaling
            pi.setLabel("left", f"{ch.name}  ({ch.unit})")
            # width-1 non-AA pen: width-2 AA streaming curves hit Qt's slow
            # path stroker when repainting embedded (see strainbook plots)
            self._curves[ch.name] = pi.plot(
                [], [], antialias=False,
                pen=pg.mkPen(theme.series_color(i), width=1))
            if self._plots:
                pw.setXLink(self._plots[0])
                self._plots[-1].getPlotItem().getAxis("bottom") \
                    .setStyle(showValues=False)
            self._lay.addWidget(pw, 1)
            self._plots.append(pw)
        if self._plots:
            self._plots[-1].getPlotItem().setLabel(
                "bottom", "time before now  (s)")

    def note_rate(self, hz: float) -> None:
        if hz > 1.0:
            self._rate = hz

    def refresh(self) -> None:
        if self.paused or self._ring is None or not self._curves:
            return
        if not self.isVisible():          # tab not shown — skip the work
            return
        n = int(self.window_s * self._rate * 1.05) + 2
        data = self._ring.tail(n)
        t = data["t"]
        if t.size < 2:
            return
        x = t - t[-1]
        keep = x >= -self.window_s
        x = x[keep]
        for name, curve in self._curves.items():
            field = f"{name}_V" if self.show_volts else name
            if field in data:
                xd, yd = _envelope(x, data[field][keep])
                curve.setData(xd, yd)
        if self._plots:
            self._plots[0].getPlotItem().setXRange(-self.window_s, 0.0,
                                                   padding=0)

    def set_show_volts(self, show: bool) -> None:
        self.show_volts = show
        for name, pw in zip(self._curves, self._plots):
            unit = "V" if show else self._units.get(name, "")
            pw.getPlotItem().setLabel("left", f"{name}  ({unit})")
