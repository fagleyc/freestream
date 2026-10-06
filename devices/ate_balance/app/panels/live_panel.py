"""Live panel — real-time bar graph of the six loads + device status strip.

Shows exactly what the balance itself reports: wind-axis loads, drive
positions, stream rate and packet health.  (Aerodynamic coefficients and
tunnel conditions are deliberately absent — this device does not measure
them; they belong to the integrated AeroVIS reduction.)
"""

from __future__ import annotations

from typing import Dict, Optional

import numpy as np
from PyQt6.QtWidgets import (
    QGroupBox, QHBoxLayout, QLabel, QVBoxLayout, QWidget,
)

from ate_balance import theme
from ate_balance.datamodel import RingBuffer
from ate_balance.app.plots import LoadBars


def _stat(title: str, unit: str = "") -> tuple[QWidget, QLabel]:
    """A compact titled readout for the status strip."""
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(10, 2, 10, 2)
    lay.setSpacing(0)
    t = QLabel(title)
    t.setObjectName("dim")
    val = QLabel("--")
    val.setObjectName("mono")
    val.setStyleSheet(f"font-family: Consolas, monospace; font-size: 13pt; "
                      f"color: {theme.TEXT};")
    lay.addWidget(t)
    lay.addWidget(val)
    if unit:
        u = QLabel(unit)
        u.setObjectName("unit")
        lay.addWidget(u)
    return box, val


class LivePanel(QWidget):
    """Bar-graph view of the live load stream, smoothed over a short window."""

    def __init__(self, ring: RingBuffer, parent=None):
        super().__init__(parent)
        self._ring = ring
        self.avg_ms = 50            # smoothing window; kept in sync w/ config
        #: display-only low-pass cutoff (Hz); widens the averaging window
        #: to 1/cutoff when that is longer than ``avg_ms``. Never touches
        #: the ring or anything recorded.
        self.lpf_hz = 0.0
        #: rated maxima IN THE STREAMED UNITS (the host converts); 0 = none
        self.max_loads: Dict[str, float] = {}
        self._rate = 0.0
        self._build()

    def set_units(self, force_u: str, moment_u: str) -> None:
        """Relabel the bar axes for the configured unit system."""
        self.bars.set_units(force_u, moment_u)

    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        bars_box = QGroupBox("Wind-axis loads (live)")
        bl = QVBoxLayout(bars_box)
        self.bars = LoadBars()
        bl.addWidget(self.bars)
        # overstress hint — shown only while a load exceeds a nonzero
        # rated maximum (Settings → Rated load maxima)
        self.over_lbl = QLabel("")
        self.over_lbl.setStyleSheet(
            f"color: {theme.ERROR}; font-weight: bold;")
        self.over_lbl.setVisible(False)
        bl.addWidget(self.over_lbl)
        root.addWidget(bars_box, 1)

        strip = QGroupBox("Device status")
        sl = QHBoxLayout(strip)
        sl.setSpacing(4)
        self._vals: Dict[str, QLabel] = {}
        self._stat_boxes: Dict[str, QWidget] = {}
        for key, title, unit in (
            ("yaw", "Yaw position", "deg"),
            ("inc", "Incidence position", "deg"),
            ("rate", "Stream rate", "Hz"),
            ("sync", "Sync input", ""),
            ("avg", "Bar smoothing", "ms"),
            ("q", "Tunnel q (DaqBook)", "Pa"),
        ):
            w, lbl = _stat(title, unit)
            self._vals[key] = lbl
            self._stat_boxes[key] = w
            sl.addWidget(w)
        self._stat_boxes["q"].setVisible(False)   # only with a real source
        sl.addStretch(1)
        root.addWidget(strip)

    # ── UI-timer refresh: smooth the last ``avg_ms`` of frames into bars ──
    def refresh(self) -> None:
        rate = max(self._rate, 60.0)
        window_ms = self.avg_ms
        if self.lpf_hz > 0:
            window_ms = max(window_ms, 1000.0 / self.lpf_hz)
        n = max(2, int(window_ms / 1000.0 * rate))
        data = self._ring.tail(n)
        if data["t"].size == 0:
            return
        loads = {c: float(np.mean(data[c]))
                 for c in ("Fx", "Fy", "Fz", "Mx", "My", "Mz")}
        self.bars.update_loads(loads)
        over = [c for c, v in loads.items()
                if self.max_loads.get(c, 0.0) > 0.0
                and abs(v) > self.max_loads[c]]
        if over:
            self.over_lbl.setText(
                "OVERSTRESS — above rated maximum: " + ", ".join(over))
        self.over_lbl.setVisible(bool(over))
        self._vals["sync"].setText(str(int(data["sync"][-1])))
        self._vals["avg"].setText(f"{window_ms:.0f}")

    # ── slow-path updates ──
    def set_rate(self, hz: float) -> None:
        self._rate = hz
        self._vals["rate"].setText(f"{hz:6.1f}")

    def set_positions(self, yaw: float, inc: float) -> None:
        self._vals["yaw"].setText(f"{yaw:+7.2f}")
        self._vals["inc"].setText(f"{inc:+7.2f}")

    # ── tunnel q from a real aux source (hidden unless one is attached) ──
    def show_q(self, visible: bool) -> None:
        self._stat_boxes["q"].setVisible(visible)

    def set_q(self, q_pa: Optional[float]) -> None:
        self._vals["q"].setText("--" if q_pa is None else f"{q_pa:8.1f}")
