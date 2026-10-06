"""Vertical, signed balance-element load bars for the Forces page.

Each bar is one calibrated element (N1 .. Mx, or the external balance's
Fx .. Mz) drawn as a fraction of its RATED maximum on a shared scale of
+-120 %:

* a zero line in the middle; the fill grows UP for a positive load and
  DOWN for a negative one, coloured by how close it is to the limit
  (green / amber / red) and gently animated so it never jumps;
* the +-100 % rated limits as red dashed lines, the warning threshold
  as an amber band beyond +-warn;
* the rolling 30 s maximum and minimum as bright markers with a faint
  envelope between them (reset on tare);
* an arrow cap when a load runs off the scale.

:class:`LoadBarScale` is the matching % axis drawn once at the left of
the row. Pure display - nothing here feeds the record interlock.
"""

from __future__ import annotations

from typing import Optional

from PyQt6.QtCore import QPointF, QRectF, Qt, QTimer
from PyQt6.QtGui import (QBrush, QColor, QFont, QLinearGradient, QPainter,
                         QPainterPath, QPen, QPolygonF)
from PyQt6.QtWidgets import QSizePolicy, QWidget

from .. import theme

#: full bar half-height = 120 % of the rated load
SPAN = 1.2
#: vertical padding (px) above +SPAN and below -SPAN; shared with the scale
PAD = 10


def _y_at(frac: float, top: float, bottom: float) -> float:
    """Pixel y of a signed load fraction (0 = mid, +SPAN = top)."""
    mid = 0.5 * (top + bottom)
    half = 0.5 * (bottom - top)
    f = max(-SPAN, min(SPAN, frac))
    return mid - f / SPAN * half


class LoadBar(QWidget):
    """One vertical signed load bar (see module docstring).

    ``set_load(u, peak, color, ...)`` keeps the original signature:
    ``u`` is |load| / rated (None = no rated max, no fill) and ``peak`` the
    rolling peak of |u|. The signed extras drive the new drawing:
    ``frac`` (signed u), ``peak_hi`` / ``peak_lo`` (signed rolling max /
    min over the hold window) and ``warn`` (the amber threshold).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(44, 140)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self._u: Optional[float] = None
        self._peak: Optional[float] = None
        self._frac: Optional[float] = None
        self._peak_hi: Optional[float] = None
        self._peak_lo: Optional[float] = None
        self._warn = 0.8
        self._color = theme.SUCCESS
        self._shown: Optional[float] = None       # animated fill fraction
        self._anim = QTimer(self)
        self._anim.setInterval(16)
        self._anim.timeout.connect(self._step)

    # ── data ────────────────────────────────────────────────────────────
    def set_load(self, u: Optional[float], peak: Optional[float],
                 color: str, *, frac: Optional[float] = None,
                 peak_hi: Optional[float] = None,
                 peak_lo: Optional[float] = None,
                 warn: Optional[float] = None) -> None:
        if frac is None and u is not None:
            frac = u
        state = (u, peak, color, frac, peak_hi, peak_lo, warn)
        if state == (self._u, self._peak, self._color, self._frac,
                     self._peak_hi, self._peak_lo, self._warn):
            return
        self._u, self._peak, self._color = u, peak, color
        self._frac, self._peak_hi, self._peak_lo = frac, peak_hi, peak_lo
        if warn is not None:
            self._warn = warn
        if frac is None:
            self._shown = None
        elif self._shown is None:
            self._shown = frac
        elif not self._anim.isActive():
            self._anim.start()
        self.update()

    def _step(self) -> None:
        """Ease the drawn fill toward the live value (~0.1 s)."""
        if self._frac is None or self._shown is None:
            self._anim.stop()
            return
        diff = self._frac - self._shown
        if abs(diff) < 1e-4:
            self._shown = self._frac
            self._anim.stop()
        else:
            self._shown += 0.3 * diff
        self.update()

    # ── drawing ─────────────────────────────────────────────────────────
    def paintEvent(self, _ev) -> None:                 # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        top, bottom = r.top() + PAD, r.bottom() - PAD
        track_w = min(46.0, max(18.0, r.width() * 0.5))
        x0 = r.center().x() - track_w / 2
        track = QRectF(x0, top, track_w, bottom - top)
        dark = theme.current().dark

        # track
        p.setPen(QPen(QColor(theme.BORDER), 1))
        p.setBrush(QColor(theme.BG_LIGHTER))
        p.drawRoundedRect(track, 7, 7)

        # warning (amber) and over-limit (red) bands, both signs
        warn = max(0.0, min(self._warn, 1.0))
        for lo, hi, col, a in ((warn, 1.0, theme.WARNING, 38),
                               (1.0, SPAN, theme.ERROR, 46)):
            band = QColor(col)
            band.setAlpha(a)
            for sgn in (1, -1):
                y1, y2 = _y_at(sgn * lo, top, bottom), \
                    _y_at(sgn * hi, top, bottom)
                p.fillRect(QRectF(x0 + 1, min(y1, y2), track_w - 2,
                                  abs(y2 - y1)), band)

        # quarter grid
        grid = QColor(theme.TEXT_DIM)
        grid.setAlpha(45)
        p.setPen(QPen(grid, 1))
        for f in (0.25, 0.5, 0.75, -0.25, -0.5, -0.75):
            y = _y_at(f, top, bottom)
            p.drawLine(QPointF(x0 + 4, y), QPointF(x0 + track_w - 4, y))

        envelope = self._frac is not None and self._peak_hi is not None \
            and self._peak_lo is not None
        # rolling 30 s envelope (faint) between the min and max markers
        if envelope:
            env = QColor(theme.TEXT)
            env.setAlpha(26 if dark else 22)
            y_hi = _y_at(self._peak_hi, top, bottom)
            y_lo = _y_at(self._peak_lo, top, bottom)
            p.fillRect(QRectF(x0 + 2, min(y_hi, y_lo), track_w - 4,
                              abs(y_lo - y_hi)), env)

        # the fill, from zero toward the (animated) value
        mid = _y_at(0.0, top, bottom)
        if self._shown is not None and self._u is not None:
            f = self._shown
            y = _y_at(f, top, bottom)
            fill_w = track_w * 0.62
            fx = r.center().x() - fill_w / 2
            rect = QRectF(fx, min(mid, y), fill_w, max(abs(y - mid), 1.0))
            base = QColor(self._color)
            tip = QColor(self._color).lighter(135 if dark else 115)
            grad = QLinearGradient(QPointF(0, mid), QPointF(0, y))
            grad.setColorAt(0.0, base)
            grad.setColorAt(1.0, tip)
            if abs(f) >= warn:                       # soft glow near limit
                glow = QColor(self._color)
                glow.setAlpha(60)
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(glow)
                p.drawRoundedRect(rect.adjusted(-4, -3, 4, 3), 6, 6)
            path = QPainterPath()
            path.addRoundedRect(rect, 4, 4)
            p.fillPath(path, QBrush(grad))
            if abs(f) > SPAN:                         # off-scale arrow cap
                sgn = 1 if f > 0 else -1
                ya = _y_at(sgn * SPAN, top, bottom)
                cx = r.center().x()
                tri = QPolygonF([QPointF(cx - 9, ya + sgn * 8),
                                 QPointF(cx + 9, ya + sgn * 8),
                                 QPointF(cx, ya - sgn * 4)])
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(QColor(theme.ERROR))
                p.drawPolygon(tri)

        # zero line
        p.setPen(QPen(QColor(theme.TEXT_DIM), 1.6))
        p.drawLine(QPointF(x0 - 4, mid), QPointF(x0 + track_w + 4, mid))

        # +-100 % rated limits
        lim = QPen(QColor(theme.ERROR), 1.6, Qt.PenStyle.DashLine)
        p.setPen(lim)
        for sgn in (1, -1):
            y = _y_at(sgn * 1.0, top, bottom)
            p.drawLine(QPointF(x0 - 6, y), QPointF(x0 + track_w + 6, y))

        # rolling max / min markers (bright tick + pointer)
        if envelope:
            for val in (self._peak_hi, self._peak_lo):
                col = QColor(theme.ERROR if abs(val) >= 1.0 else theme.TEXT)
                y = _y_at(val, top, bottom)
                p.setPen(QPen(col, 2.2))
                p.drawLine(QPointF(x0 - 2, y), QPointF(x0 + track_w + 2, y))
                p.setPen(Qt.PenStyle.NoPen)
                p.setBrush(col)
                p.drawPolygon(QPolygonF([QPointF(x0 + track_w + 3, y),
                                         QPointF(x0 + track_w + 9, y - 4),
                                         QPointF(x0 + track_w + 9, y + 4)]))
        elif self._u is None:
            # no rated maximum: say so in the track instead of a fake fill
            p.setPen(QColor(theme.TEXT_DISABLED))
            f = QFont(self.font())
            f.setPointSizeF(max(f.pointSizeF() - 1.5, 6.5))
            p.setFont(f)
            p.save()
            p.translate(r.center().x(), 0.5 * (top + bottom))
            p.rotate(-90)
            p.drawText(QRectF(-60, -9, 120, 18),
                       Qt.AlignmentFlag.AlignCenter, "no rated max")
            p.restore()
        p.end()


class LoadBarScale(QWidget):
    """The shared % axis for a row of :class:`LoadBar` (same geometry)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(46)
        self.setMinimumHeight(140)
        self.setSizePolicy(QSizePolicy.Policy.Fixed,
                           QSizePolicy.Policy.Expanding)

    def paintEvent(self, _ev) -> None:                 # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        top, bottom = r.top() + PAD, r.bottom() - PAD
        f = QFont(self.font())
        f.setPointSizeF(max(f.pointSizeF() - 1.5, 6.5))
        p.setFont(f)
        for frac in (1.0, 0.5, 0.0, -0.5, -1.0):
            y = _y_at(frac, top, bottom)
            col = theme.ERROR if abs(frac) == 1.0 else theme.TEXT_DIM
            p.setPen(QColor(col))
            text = "0" if frac == 0 else f"{frac * 100:+.0f}%"
            p.drawText(QRectF(r.left(), y - 8, r.width() - 8, 16),
                       Qt.AlignmentFlag.AlignRight
                       | Qt.AlignmentFlag.AlignVCenter, text)
            p.drawLine(QPointF(r.right() - 5, y), QPointF(r.right(), y))
        p.end()
