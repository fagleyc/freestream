"""pyqtgraph widgets for the NI USB-6351 GUI.

* :class:`ChannelTiles`   - stat tile per channel (engineering units).
* :class:`ChannelHistory` - one plot with ALL enabled AI channels overlaid
  in raw volts (the balance bridges sit at mV level but share the volt
  axis naturally; wheel-zoom digs into them), drawn from the device ring
  buffer with envelope decimation so redraws stay light at any
  rate/window.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ni_usb_6351 import theme
from ni_usb_6351.config import ChannelConfig
from ni_usb_6351.datamodel import ScanRingBuffer

theme.apply_pyqtgraph_theme()

_MAX_PLOT_BINS = 1200


def _envelope(x: np.ndarray, y: np.ndarray, max_bins: int = _MAX_PLOT_BINS):
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


def _style_plot(pw: pg.PlotWidget) -> None:
    pi = pw.getPlotItem()
    pi.showGrid(x=False, y=True, alpha=0.25)
    # fully interactive: wheel-zoom, drag-pan, right-click menu (axis
    # limits, autorange, export…)
    pi.setMenuEnabled(True)
    pi.setMouseEnabled(x=True, y=True)
    pi.setClipToView(True)
    for side in ("left", "bottom"):
        ax = pi.getAxis(side)
        ax.setPen(pg.mkPen(theme.AXIS, width=1))
        ax.setTextPen(theme.TEXT_DIM)
        ax.enableAutoSIPrefix(False)


class _Tile(QFrame):
    def __init__(self, name: str, unit: str, color: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"QFrame {{ background-color: {theme.BG_LIGHT}; "
            f"border: 1px solid {theme.BORDER}; border-radius: 6px; }}"
            f"QLabel {{ border: none; background: transparent; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
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
            "font-family: 'Segoe UI', sans-serif; font-size: 18pt; "
            f"font-weight: 600; color: {theme.TEXT};")
        lay.addWidget(self.value)
        self.sub = QLabel(unit)
        self.sub.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(self.sub)
        self._unit = unit

    def update_value(self, eng: float) -> None:
        mag = abs(eng)
        decimals = 4 if mag < 1 else (3 if mag < 100 else 2)
        self.value.setText(f"{eng:+,.{decimals}f}")
        self.sub.setText(self._unit)


class ChannelTiles(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.avg_ms = 200
        self._tiles: Dict[str, _Tile] = {}
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(6)

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
        data = ring.tail(n, fields=["t", *self._tiles])
        if data["t"].size == 0:
            return
        for name, tile in self._tiles.items():
            if name in data:
                tile.update_value(float(np.mean(data[name])))


class ChannelHistory(QWidget):
    """All enabled AI channels overlaid in raw volts."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.window_s = 30.0
        self.paused = False
        self.follow = True        # x pinned to now; user zoom/pan unpins
        self._rate = 200.0
        self._ring: Optional[ScanRingBuffer] = None
        self._curves: Dict[str, pg.PlotDataItem] = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self._plot = pg.PlotWidget()
        _style_plot(self._plot)
        pi = self._plot.getPlotItem()
        pi.setLabel("left", "input  (V)")
        pi.setLabel("bottom", "time before now  (s)")
        pi.addLegend(offset=(8, 8), labelTextColor=theme.TEXT,
                     brush=pg.mkBrush(theme.PLOT_BG + "cc"),
                     pen=pg.mkPen(theme.BORDER))
        pi.getViewBox().sigRangeChangedManually.connect(self._user_zoomed)
        lay.addWidget(self._plot, 1)

    def set_channels(self, channels: List[ChannelConfig],
                     ring: Optional[ScanRingBuffer]) -> None:
        self._ring = ring
        pi = self._plot.getPlotItem()
        pi.clearPlots()
        self._curves = {}
        for i, ch in enumerate(channels):
            # Streaming curves MUST use width-1 non-antialiased pens: a
            # width-2 AA pen forces Qt's slow path stroker when the plot
            # repaints through the parent backing store — measured 3.2 s
            # per repaint (vs 20 ms at width 1) on a full 2400-pt window.
            pen = pg.mkPen(theme.series_color(i), width=1)
            self._curves[ch.name] = pi.plot([], [], name=ch.name, pen=pen,
                                            antialias=False)

    def note_rate(self, hz: float) -> None:
        if hz > 1.0:
            self._rate = hz

    def _user_zoomed(self, *_a) -> None:
        self.follow = False       # stop pinning x; "Follow" button restores

    def set_follow(self, follow: bool) -> None:
        self.follow = follow

    def refresh(self) -> None:
        if self.paused or self._ring is None or not self.isVisible():
            return
        n = int(self.window_s * self._rate * 1.05) + 2
        data = self._ring.tail(
            n, fields=["t", *(f"{name}_V" for name in self._curves)])
        t = data["t"]
        if t.size < 2:
            return
        x = t - t[-1]
        keep = x >= -self.window_s
        x = x[keep]
        for name, curve in self._curves.items():
            key = f"{name}_V"
            if key in data:
                xd, yd = _envelope(x, data[key][keep])
                curve.setData(xd, yd)
        if self.follow:
            self._plot.getPlotItem().setXRange(-self.window_s, 0.0,
                                               padding=0)
