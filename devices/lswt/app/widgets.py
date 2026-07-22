"""Instrument widgets for the LSWT fan panel — Hz gauge, fan rotor,
stat tiles.

``HzGauge``/``FanRotor`` are self-contained adaptations of the
tunnel_plc gauge widgets (copied, not imported — device packages stay
standalone). Color-by-job: series blue = actual Hz (gauge arc + needle
+ plot line), series yellow = setpoint (gauge tick + dashed plot
line), status red = the max_hz limit tick.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PyQt6.QtWidgets import QLabel, QSizePolicy, QVBoxLayout, QWidget

from lswt import theme

_SWEEP_DEG = 270.0        # gauge arc: 225° → −45° (Qt CCW-positive)
_START_DEG = 225.0


class HzGauge(QWidget):
    """Circular fan-frequency gauge: value arc + needle, setpoint tick,
    max_hz limit tick, digital readout in the hub. Fixed 0–60 Hz range
    (the ACS530 full scale)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._actual = 0.0
        self._setpoint = 0.0
        self._max_hz = 60.0
        self._range = 60.0
        self.setMinimumSize(240, 220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def set_state(self, actual: float, setpoint: float, max_hz: float):
        if (actual, setpoint, max_hz) != (self._actual, self._setpoint,
                                          self._max_hz):
            self._actual, self._setpoint = actual, setpoint
            self._max_hz = max_hz
            self._range = max(60.0, max_hz)
            self.update()

    # ── geometry helpers ──
    def _angle(self, value: float) -> float:
        frac = min(max(value / self._range, 0.0), 1.0)
        return _START_DEG - _SWEEP_DEG * frac

    @staticmethod
    def _radial(center: QPointF, angle_deg: float, r: float) -> QPointF:
        a = math.radians(angle_deg)
        return QPointF(center.x() + r * math.cos(a),
                       center.y() - r * math.sin(a))

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        side = min(self.width(), self.height() * 1.08)
        cx, cy = self.width() / 2.0, self.height() * 0.54
        center = QPointF(cx, cy)
        radius = side * 0.40
        arc_w = max(radius * 0.10, 8.0)
        rect = QRectF(cx - radius, cy - radius, radius * 2, radius * 2)

        # track (recessive)
        p.setPen(QPen(QColor(theme.SURFACE), arc_w, Qt.PenStyle.SolidLine,
                      Qt.PenCapStyle.FlatCap))
        p.drawArc(rect, int(_START_DEG * 16), int(-_SWEEP_DEG * 16))

        # value arc (series blue — same entity color as the plot line)
        frac = min(max(self._actual / self._range, 0.0), 1.0)
        if frac > 0:
            p.setPen(QPen(QColor(theme.series_color(0)), arc_w,
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.FlatCap))
            p.drawArc(rect, int(_START_DEG * 16),
                      int(-_SWEEP_DEG * frac * 16))

        # major ticks + labels
        p.setFont(QFont("Segoe UI", max(int(side * 0.028), 7)))
        for i in range(7):
            v = self._range * i / 6.0
            ang = self._angle(v)
            p.setPen(QPen(QColor(theme.AXIS), 2))
            p.drawLine(self._radial(center, ang, radius + arc_w * 0.7),
                       self._radial(center, ang, radius + arc_w * 1.35))
            p.setPen(QColor(theme.TEXT_DIM))
            lbl = f"{v:g}"
            pos = self._radial(center, ang, radius + arc_w * 2.6)
            fm = p.fontMetrics()
            p.drawText(QPointF(pos.x() - fm.horizontalAdvance(lbl) / 2,
                               pos.y() + fm.ascent() / 2.5), lbl)

        # max_hz limit tick (status red — a state boundary)
        if 0 < self._max_hz < self._range:
            ang = self._angle(self._max_hz)
            p.setPen(QPen(QColor(theme.ERROR), 3))
            p.drawLine(self._radial(center, ang, radius - arc_w),
                       self._radial(center, ang, radius + arc_w * 1.2))

        # setpoint tick (series yellow — same entity as the dashed line)
        ang_sp = self._angle(self._setpoint)
        p.setPen(QPen(QColor(theme.series_color(2)), 3))
        p.drawLine(self._radial(center, ang_sp, radius - arc_w * 1.4),
                   self._radial(center, ang_sp, radius + arc_w * 0.9))

        # needle (actual)
        ang_n = self._angle(self._actual)
        tip = self._radial(center, ang_n, radius - arc_w * 1.1)
        base_l = self._radial(center, ang_n + 90, radius * 0.035)
        base_r = self._radial(center, ang_n - 90, radius * 0.035)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(theme.series_color(0)))
        p.drawPolygon(QPolygonF([tip, base_l, base_r]))
        p.setBrush(QColor(theme.BG_LIGHTER))
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawEllipse(center, radius * 0.07, radius * 0.07)

        # hub readout
        p.setPen(QColor(theme.TEXT))
        p.setFont(QFont("Segoe UI", max(int(side * 0.085), 12),
                        QFont.Weight.DemiBold))
        fm = p.fontMetrics()
        val = f"{self._actual:.1f}"
        p.drawText(QPointF(cx - fm.horizontalAdvance(val) / 2,
                           cy + radius * 0.48), val)
        p.setPen(QColor(theme.TEXT_DIM))
        p.setFont(QFont("Segoe UI", max(int(side * 0.032), 8)))
        fm = p.fontMetrics()
        p.drawText(QPointF(cx - fm.horizontalAdvance("Hz") / 2,
                           cy + radius * 0.48 + fm.height()), "Hz")
        sp = f"set {self._setpoint:.1f}"
        p.setPen(QColor(theme.series_color(2)))
        fm = p.fontMetrics()
        p.drawText(QPointF(cx - fm.horizontalAdvance(sp) / 2,
                           cy + radius * 0.85 + fm.height()), sp)


class FanRotor(QWidget):
    """Small spinning rotor — rotation rate follows the actual Hz.

    Purely a motion cue for "fan running" (the gauge carries the
    value); dim and static when stopped.
    """

    def __init__(self, parent=None, diameter: int = 44):
        super().__init__(parent)
        self._hz = 0.0
        self._running = False
        self._angle = 0.0
        self.setFixedSize(diameter, diameter)
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._advance)

    def set_state(self, hz: float, running: bool):
        self._hz = hz
        self._running = running
        want = running and hz > 0.05
        if want and not self._timer.isActive():
            self._timer.start()
        elif not want and self._timer.isActive():
            self._timer.stop()
            self.update()

    def _advance(self):
        # visual rate: full turn ≈ 1 s at 60 Hz, capped for legibility
        self._angle = (self._angle +
                       min(self._hz, 120.0) * 6.0 * 0.033) % 360.0
        self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = min(self.width(), self.height()) / 2.0
        p.translate(self.width() / 2.0, self.height() / 2.0)
        color = QColor(theme.series_color(0)) if self._running \
            else QColor(theme.TEXT_DISABLED)
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(QPointF(0, 0), r - 1, r - 1)
        p.rotate(-self._angle)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(color)
        blade = QPolygonF([QPointF(0, 0),
                           QPointF(r * 0.28, -r * 0.72),
                           QPointF(-r * 0.28, -r * 0.72)])
        for _ in range(3):
            p.rotate(120)
            p.drawPolygon(blade)
        p.setBrush(QColor(theme.BG_LIGHTER))
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.drawEllipse(QPointF(0, 0), r * 0.18, r * 0.18)


class StatTile(QWidget):
    """Small dashboard tile: dim caption over a big monospace value."""

    def __init__(self, caption: str, unit: str = "", parent=None):
        super().__init__(parent)
        col = QVBoxLayout(self)
        col.setContentsMargins(10, 6, 10, 6)
        col.setSpacing(0)
        self.caption = QLabel(caption)
        self.caption.setObjectName("dim")
        col.addWidget(self.caption)
        self.value = QLabel("--")
        self.value.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 20pt; "
            f"font-weight: 600; color: {theme.TEXT};")
        col.addWidget(self.value)
        self.unit = QLabel(unit)
        self.unit.setObjectName("dim")
        col.addWidget(self.unit)
        self.setStyleSheet(
            f"StatTile {{ background-color: {theme.BG_LIGHTER}; "
            f"border: 1px solid {theme.BORDER}; border-radius: 6px; }}")

    def set_value(self, text: str, color: str = None):
        self.value.setText(text)
        self.value.setStyleSheet(
            "font-family: Consolas, monospace; font-size: 20pt; "
            f"font-weight: 600; color: {color or theme.TEXT};")

    def set_unit(self, unit: str):
        self.unit.setText(unit)
