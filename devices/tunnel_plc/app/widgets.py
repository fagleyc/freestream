"""Instrument widgets for the tunnel panel — gauge, LEDs, fan rotor.

Color-by-job, matching the time-history plot so identity follows the
entity across the whole panel: series blue = Actual RPM (gauge arc +
needle + plot line), series yellow = RPM setpoint (gauge tick + dashed
plot line). Status colors (green/red/amber) are reserved for the LED
states and always carry a text label — never color alone. Text wears
text tokens; the marks carry the color.
"""

from __future__ import annotations

import math

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF, QRadialGradient
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

from tunnel_plc import theme

_SWEEP_DEG = 270.0        # gauge arc: 225° → −45° (Qt CCW-positive)
_START_DEG = 225.0


def _nice_range(needed: float) -> float:
    """Smallest 'nice' full-scale (1/2/2.5/5 ×10^k) covering ``needed``."""
    if needed <= 0:
        return 1000.0
    exp = math.floor(math.log10(needed))
    for mult in (1.0, 2.0, 2.5, 5.0, 10.0):
        candidate = mult * 10 ** exp
        if candidate >= needed:
            return candidate
    return 10.0 ** (exp + 1)


class RpmGauge(QWidget):
    """Circular fan-speed gauge: value arc + needle, setpoint tick,
    rpm_max limit tick, digital readout in the hub."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._actual = 0.0
        self._setpoint = 0.0
        self._rpm_max = 0.0
        self._range = 1000.0
        self.setMinimumSize(240, 220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def set_state(self, actual: float, setpoint: float, rpm_max: float):
        needed = max(actual, setpoint, rpm_max, 1.0) * 1.02
        rng = _nice_range(needed)
        if (actual, setpoint, rpm_max, rng) != (
                self._actual, self._setpoint, self._rpm_max, self._range):
            self._actual, self._setpoint = actual, setpoint
            self._rpm_max, self._range = rpm_max, rng
            self.update()

    # ── geometry helpers ──
    def _angle(self, value: float) -> float:
        """Gauge angle (deg, Qt convention) for a value."""
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

        # major ticks + labels (recessive ink, text tokens)
        p.setFont(QFont("Segoe UI", max(int(side * 0.028), 7)))
        for i in range(6):
            v = self._range * i / 5.0
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

        # rpm_max limit tick (status red — a state boundary)
        if self._rpm_max > 0:
            ang = self._angle(self._rpm_max)
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

        # hub readout (text tokens, not series color)
        p.setPen(QColor(theme.TEXT))
        p.setFont(QFont("Segoe UI", max(int(side * 0.085), 12),
                        QFont.Weight.DemiBold))
        fm = p.fontMetrics()
        val = f"{self._actual:g}"
        p.drawText(QPointF(cx - fm.horizontalAdvance(val) / 2,
                           cy + radius * 0.48), val)
        p.setPen(QColor(theme.TEXT_DIM))
        p.setFont(QFont("Segoe UI", max(int(side * 0.032), 8)))
        fm = p.fontMetrics()
        p.drawText(QPointF(cx - fm.horizontalAdvance("RPM") / 2,
                           cy + radius * 0.48 + fm.height()), "RPM")
        sp = f"set {self._setpoint:g}"
        p.setPen(QColor(theme.series_color(2)))
        fm = p.fontMetrics()
        p.drawText(QPointF(cx - fm.horizontalAdvance(sp) / 2,
                           cy + radius * 0.85 + fm.height()), sp)


class FanRotor(QWidget):
    """Small spinning rotor — rotation rate follows the actual RPM.

    Purely a motion cue for Fan_Running (the gauge carries the value);
    dim and static when stopped.
    """

    def __init__(self, parent=None, diameter: int = 44):
        super().__init__(parent)
        self._rpm = 0.0
        self._running = False
        self._angle = 0.0
        self.setFixedSize(diameter, diameter)
        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._advance)

    def set_state(self, rpm: float, running: bool):
        self._rpm = rpm
        self._running = running
        want = running and rpm > 0
        if want and not self._timer.isActive():
            self._timer.start()
        elif not want and self._timer.isActive():
            self._timer.stop()
            self.update()

    def _advance(self):
        # visual rate: full turn ≈ 1 s at 600 RPM, capped for legibility
        self._angle = (self._angle +
                       min(self._rpm, 1200.0) * 0.6 * 0.033) % 360.0
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


class _LedDot(QWidget):
    """The round indicator itself (glow when lit)."""

    def __init__(self, parent=None, diameter: int = 16):
        super().__init__(parent)
        self._lit = False
        self._color = QColor(theme.SUCCESS)
        self.setFixedSize(diameter, diameter)

    def set_state(self, lit: bool, color: str):
        c = QColor(color)
        if lit != self._lit or c != self._color:
            self._lit, self._color = lit, c
            self.update()

    def paintEvent(self, _event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = min(self.width(), self.height()) / 2.0
        center = QPointF(self.width() / 2.0, self.height() / 2.0)
        if self._lit:
            glow = QRadialGradient(center, r)
            bright = QColor(self._color)
            glow.setColorAt(0.0, bright.lighter(135))
            glow.setColorAt(0.55, bright)
            edge = QColor(bright)
            edge.setAlpha(0)
            glow.setColorAt(1.0, edge)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(glow)
            p.drawEllipse(center, r, r)
            p.setPen(QPen(bright.darker(120), 1))
            p.setBrush(bright)
            p.drawEllipse(center, r * 0.62, r * 0.62)
        else:
            p.setPen(QPen(QColor(theme.BORDER), 1))
            p.setBrush(QColor(theme.BG_LIGHTER))
            p.drawEllipse(center, r * 0.62, r * 0.62)


class LedLamp(QWidget):
    """LED + text label (state is never color alone)."""

    def __init__(self, label: str, bad_when_lit: bool, parent=None):
        super().__init__(parent)
        self._bad = bad_when_lit
        row = QHBoxLayout(self)
        row.setContentsMargins(2, 1, 2, 1)
        row.setSpacing(7)
        self._dot = _LedDot()
        row.addWidget(self._dot)
        self._label = QLabel(label)
        row.addWidget(self._label)
        row.addStretch(1)
        self.set_lit(False)

    def set_lit(self, lit: bool):
        color = theme.ERROR if self._bad else theme.SUCCESS
        self._dot.set_state(lit, color)
        if lit:
            ink = theme.ERROR if self._bad else theme.TEXT
            weight = "bold" if self._bad else "normal"
            self._label.setStyleSheet(f"color: {ink}; "
                                      f"font-weight: {weight};")
        else:
            self._label.setStyleSheet(f"color: {theme.TEXT_DIM};")
