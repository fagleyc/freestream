"""pyqtgraph widgets for the balance GUI.

* :class:`LoadBars`    - live bar graph of the six loads (forces / moments
  split into two plots so N and N·m never share an axis).
* :class:`TimeHistory` - full-rate scrolling time history of the six loads,
  fed straight from the :class:`~ate_balance.datamodel.RingBuffer` so every
  300 Hz frame is drawn, not just the UI-rate subsample.

Channel colors come from :data:`ate_balance.theme.SERIES` so a channel looks
the same in every panel.
"""

from __future__ import annotations

import math
from typing import Dict, Optional, Sequence

import numpy as np
import pyqtgraph as pg
from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout, QWidget

from ate_balance import theme
from ate_balance.datamodel import RingBuffer

theme.apply_pyqtgraph_theme()

FORCES = ("Lift", "Drag", "Side")     # N
MOMENTS = ("Roll", "Pitch", "Yaw")    # N·m

_GRID_PEN = pg.mkPen(theme.GRID, width=1)
_AXIS_PEN = pg.mkPen(theme.AXIS, width=1)
_ZERO_PEN = pg.mkPen(theme.TEXT_DIM, width=1)


def _nice_ceiling(x: float) -> float:
    """Smallest 1/2/5×10^n ≥ x (for calm axis limits)."""
    if x <= 0:
        return 1.0
    exp = math.floor(math.log10(x))
    for m in (1.0, 2.0, 5.0, 10.0):
        c = m * 10.0 ** exp
        if c >= x:
            return c
    return 10.0 ** (exp + 1)


def _style_plot(pw: pg.PlotWidget) -> None:
    pi = pw.getPlotItem()
    pi.showGrid(x=False, y=True, alpha=0.25)
    for side in ("left", "bottom"):
        ax = pi.getAxis(side)
        ax.setPen(_AXIS_PEN)
        ax.setTextPen(theme.TEXT_DIM)
        ax.enableAutoSIPrefix(False)   # no "(x0.001)" axis scaling
    pw.setBackground(theme.PLOT_BG)


# ═════════════════════════════════════════════════════════════════════════
#  Live bar graph
# ═════════════════════════════════════════════════════════════════════════

class _BarGroup(pg.PlotWidget):
    """One bar plot for a group of channels sharing a unit."""

    def __init__(self, channels: Sequence[str], unit: str, parent=None):
        super().__init__(parent)
        self._channels = tuple(channels)
        self._peak = 1.0                      # decaying autoscale peak

        _style_plot(self)
        pi = self.getPlotItem()
        pi.setMenuEnabled(False)
        pi.setMouseEnabled(x=False, y=False)
        pi.hideButtons()
        pi.setLabel("left", unit)

        xs = list(range(len(self._channels)))
        brushes = [pg.mkBrush(theme.SERIES[c]) for c in self._channels]
        self._bars = pg.BarGraphItem(
            x=xs, height=[0.0] * len(xs), width=0.55,
            brushes=brushes, pen=pg.mkPen(None))
        pi.addItem(self._bars)
        pi.addLine(y=0, pen=_ZERO_PEN)

        # channel names as x ticks; live values as text above/below each bar
        ax = pi.getAxis("bottom")
        ax.setTicks([[(i, c) for i, c in enumerate(self._channels)]])
        ax.setStyle(tickLength=0)
        self._value_labels = []
        for i in range(len(xs)):
            t = pg.TextItem("", color=theme.TEXT, anchor=(0.5, 1.0))
            t.setPos(i, 0)
            pi.addItem(t)
            self._value_labels.append(t)

        pi.setXRange(-0.6, len(xs) - 0.4, padding=0)
        self._set_yrange(1.0)

    def _set_yrange(self, mag: float) -> None:
        self._span = mag
        self.getPlotItem().setYRange(-mag, mag, padding=0.12)

    def update_values(self, values: Sequence[float]) -> None:
        vals = [float(v) for v in values]
        self._bars.setOpts(height=vals)

        # decaying-peak autoscale: expand instantly, shrink slowly
        cur = max((abs(v) for v in vals), default=0.0)
        self._peak = max(cur, self._peak * 0.995)
        target = _nice_ceiling(max(self._peak * 1.15, 1e-3))
        if target != self._span:
            self._set_yrange(target)

        decimals = 2 if self._span >= 10 else 3
        for t, v in zip(self._value_labels, vals):
            t.setText(f"{v:+.{decimals}f}")
            # sit the label just outside the bar end, clamped inside the view
            y = min(max(v, -self._span * 0.92), self._span * 0.92)
            t.setPos(t.pos().x(), y)
            t.setAnchor((0.5, 1.2) if v >= 0 else (0.5, -0.2))


class LoadBars(QWidget):
    """Side-by-side live bar graphs: forces (N) and moments (N·m)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self._forces = _BarGroup(FORCES, "Force  (N)")
        self._moments = _BarGroup(MOMENTS, "Moment  (N·m)")
        lay.addWidget(self._forces, 1)
        lay.addWidget(self._moments, 1)

    def update_loads(self, loads: Dict[str, float]) -> None:
        self._forces.update_values([loads.get(c, 0.0) for c in FORCES])
        self._moments.update_values([loads.get(c, 0.0) for c in MOMENTS])


# ═════════════════════════════════════════════════════════════════════════
#  Full-rate time history
# ═════════════════════════════════════════════════════════════════════════

class TimeHistory(QWidget):
    """Two stacked, x-linked scrolling plots (forces / moments) drawn from
    the ring buffer at the device's native rate (300 Hz on the rig).

    Call :meth:`refresh` from a UI timer; it pulls ``window_s`` seconds of
    frames from the ring and redraws.  pyqtgraph peak-mode downsampling keeps
    rendering cheap while preserving transients.
    """

    def __init__(self, ring: RingBuffer, parent=None):
        super().__init__(parent)
        self._ring = ring
        self.window_s = 10.0
        self.paused = False
        self._nominal_rate = 300.0            # refined live from the data

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        self._curves: Dict[str, pg.PlotDataItem] = {}
        self._plots = []
        for channels, unit in ((FORCES, "Force  (N)"),
                               (MOMENTS, "Moment  (N·m)")):
            pw = pg.PlotWidget()
            _style_plot(pw)
            pi = pw.getPlotItem()
            pi.setMenuEnabled(False)
            pi.setMouseEnabled(x=False, y=True)
            pi.setLabel("left", unit)
            pi.setDownsampling(auto=True, mode="peak")
            pi.setClipToView(True)
            pi.addLegend(offset=(8, 8), labelTextColor=theme.TEXT,
                         brush=pg.mkBrush(theme.PLOT_BG + "cc"),
                         pen=pg.mkPen(theme.BORDER))
            for c in channels:
                # width-1 non-AA pen: width-2 AA streaming curves hit Qt's
                # slow path stroker when repainting embedded (see strainbook)
                curve = pi.plot([], [], name=c, antialias=False,
                                pen=pg.mkPen(theme.SERIES[c], width=1))
                self._curves[c] = curve
            lay.addWidget(pw, 1)
            self._plots.append(pw)

        self._plots[1].setXLink(self._plots[0])
        self._plots[0].getPlotItem().getAxis("bottom").setStyle(showValues=False)
        self._plots[1].getPlotItem().setLabel("bottom", "time before now  (s)")

    # ── live update ──────────────────────────────────────────────────────
    def note_rate(self, hz: float) -> None:
        """Feed the measured stream rate so the tail size tracks reality."""
        if hz > 1.0:
            self._nominal_rate = hz

    def refresh(self) -> None:
        if self.paused:
            return
        n = int(self.window_s * self._nominal_rate * 1.05) + 2
        data = self._ring.tail(n)
        t = data["t"]
        if t.size < 2:
            return
        x = t - t[-1]                          # 0 at "now", negative history
        keep = x >= -self.window_s
        x = x[keep]
        for c in self._curves:
            self._curves[c].setData(x, data[c][keep])
        self._plots[0].getPlotItem().setXRange(-self.window_s, 0.0, padding=0)

    def clear(self) -> None:
        for c in self._curves.values():
            c.setData([], [])
