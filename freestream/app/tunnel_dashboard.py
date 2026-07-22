"""Tunnel dashboard — the ONE merged Tunnel tab (monitors + environment).

Top band: fan RPM gauge + spinning rotor (REUSED from the tunnel_plc
device app — imported, never copied), big stat tiles for the derived
flow condition (Mach, q via :mod:`freestream.derived` — the single
isentropic source) and the ambient DaqBook channels (P total, T total),
bearing temperatures (— until the driver's opt-in ``bearing_temps``
extended gateway block is enabled), and the VersaMax status-light grid
(status colors carry STATE only and always ship with a text label).

Below: the three strip charts (Mach, q, Fan RPM) on a shared, linked
time axis. Series colors follow the entity across the suite (Mach =
slot 0, q = slot 1, RPM = slot 2, exactly as the previous Tunnel tab);
values wear mono text tokens, chart chrome stays recessive.

Everything reads through the manager: the tunnel adapter's read-only
``snapshot()`` (PLC lights/RPM) and the DaqBook adapter's ``latest()``.
DISPLAY ONLY — history lives in plain deques (~120 s at 5 Hz).
"""

from __future__ import annotations

import time
from collections import deque
from typing import Dict, Optional

from PyQt6.QtCore import QPointF, QRectF, QTimer, Qt
from PyQt6.QtGui import QBrush, QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                             QLabel, QSizePolicy, QVBoxLayout, QWidget)

from .. import theme
from ..derived import (TUNNEL_CONDITION_CHANNELS, read_tunnel_conditions,
                       tunnel_state)
from ..hal import SetpointDevice

SAMPLE_MS = 200                       # 5 Hz UI sampling
HISTORY_N = 600                       # ≈120 s of history

# snapshot bool attr → (label, bad_when_lit) for the status-light grid
_LAMPS = [
    ("fan_running", "Fan running", False),
    ("inverter_fault", "Inverter fault", True),
    ("oil_level_low", "Oil level low", True),
    ("bearing_temp_low", "Bearing temp low", True),
    ("bearing_heater_on", "Bearing heater", False),
    ("console_control", "Console control", False),
]


class StatTile(QFrame):
    """One dashboard number: accent-striped card with a dim caps caption,
    a big mono value and its unit on the value's baseline.

    ``accent`` tints a slim strip on the tile's left edge (and the
    caption) — pass the entity's suite-wide series color (Mach = slot 0,
    q = slot 1, RPM = slot 2) so the tiles visually key to their strip
    charts; None keeps a neutral strip for un-charted ambients."""

    def __init__(self, label: str, unit: str = "", fmt: str = "{:.3f}",
                 value_pt: int = 16, accent: str = "", parent=None):
        super().__init__(parent)
        self._fmt = fmt
        self.setObjectName("statTile")
        # explicit minimum so the caption text never dictates the tile's
        # (and thus the whole dashboard's) minimum width — the top band
        # must be able to compress instead of pushing the central widget
        # under the Devices dock
        self.setMinimumWidth(96)
        strip = accent or theme.BORDER
        cap_color = accent or theme.TEXT_DIM
        self.setStyleSheet(
            "QFrame#statTile { background: qlineargradient("
            "x1:0, y1:0, x2:0, y2:1, "
            f"stop:0 {theme.BG_LIGHTER}, stop:1 {theme.BG_LIGHT}); "
            f"border: 1px solid {theme.BORDER}; "
            f"border-left: 3px solid {strip}; "
            "border-radius: 8px; }")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 7, 9, 8)
        lay.setSpacing(2)
        lay.addStretch(1)          # vertically center caption + value
        self._cap_text = label.upper()
        self.cap = QLabel(self._cap_text)
        self.cap.setStyleSheet(f"color: {cap_color}; font-size: 7.5pt; "
                               "font-weight: 600; letter-spacing: 1.2px; "
                               "background: transparent; border: none;")
        self.cap.setMinimumWidth(1)     # caption elides instead of forcing
        lay.addWidget(self.cap)
        row = QHBoxLayout()
        row.setSpacing(5)
        self.value = QLabel("—")
        self.value.setStyleSheet(
            "font-family: Consolas, monospace; font-weight: 600; "
            f"font-size: {value_pt}pt; color: {theme.TEXT}; "
            "background: transparent; border: none;")
        # min width 1: a long value compresses/clips INSIDE the tile
        # instead of painting past the tile border into its neighbour
        self.value.setMinimumWidth(1)
        row.addWidget(self.value)
        self.unit = QLabel(unit)
        self.unit.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 9pt; "
                                "background: transparent; border: none;")
        row.addWidget(self.unit,
                      alignment=Qt.AlignmentFlag.AlignBottom)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(1)          # keep content top-anchored in tall tiles

    def resizeEvent(self, event) -> None:              # noqa: N802
        """Elide the caption to the tile width (…) instead of clipping."""
        super().resizeEvent(event)
        fm = self.cap.fontMetrics()
        avail = max(self.width() - 22, 10)   # inner margins + slack
        self.cap.setText(fm.elidedText(
            self._cap_text, Qt.TextElideMode.ElideRight, avail))

    def set_value(self, v: Optional[float]) -> None:
        self.value.setText("—" if v is None else self._fmt.format(v))

    def set_unit(self, unit: str) -> None:
        self.unit.setText(unit)


class StatusLamp(QWidget):
    """One tunnel-status indicator chip: a glowing LED dot + label.

    Cooler than the device app's plain LedLamp row: the chip's background
    tints toward the state color when lit, the dot gets a soft radial
    halo, and the label brightens. ``bad_when_lit`` renders red when lit
    (faults); otherwise green (running/heater/console)."""

    def __init__(self, label: str, bad_when_lit: bool = False, parent=None):
        super().__init__(parent)
        self._label = label
        self._bad = bad_when_lit
        self._lit = False
        self.setMinimumSize(140, 24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def set_lit(self, lit: bool) -> None:
        lit = bool(lit)
        if lit != self._lit:
            self._lit = lit
            self.update()

    def paintEvent(self, event) -> None:               # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = float(self.width()), float(self.height())
        color = QColor(theme.ERROR if self._bad else theme.SUCCESS)

        # chip background — tinted when lit
        chip = QRectF(0.5, 1.0, w - 1.0, h - 2.0)
        bg = QColor(color) if self._lit else QColor(theme.BG)
        bg.setAlpha(30 if self._lit else 110)
        p.setPen(QPen(QColor(color if self._lit else theme.BORDER), 1.0))
        p.setBrush(QBrush(bg))
        p.drawRoundedRect(chip, h / 2 - 1, h / 2 - 1)

        # LED dot with halo
        cx, cy = 13.0, h / 2.0
        if self._lit:
            halo = QColor(color)
            halo.setAlpha(70)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(halo))
            p.drawEllipse(QPointF(cx, cy), 7.5, 7.5)
            p.setBrush(QBrush(color))
            p.setPen(QPen(QColor(theme.BG), 1.0))
            p.drawEllipse(QPointF(cx, cy), 4.2, 4.2)
            spec = QColor("#ffffff")
            spec.setAlpha(150)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(spec))
            p.drawEllipse(QPointF(cx - 1.2, cy - 1.4), 1.2, 1.2)
        else:
            p.setPen(QPen(QColor(theme.TEXT_DISABLED), 1.2))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), 4.2, 4.2)

        # label
        p.setFont(QFont("Segoe UI", 8))
        p.setPen(QColor(theme.TEXT if self._lit else theme.TEXT_DIM))
        p.drawText(QRectF(26.0, 0.0, w - 30.0, h),
                   Qt.AlignmentFlag.AlignVCenter |
                   Qt.AlignmentFlag.AlignLeft, self._label)
        p.end()


class AttitudePad(QWidget):
    """Prominent custom-painted α/β attitude crosshair.

    Horizontal axis = Beta, vertical axis = Alpha (up = +α). A bright dot
    marks the live (β, α); a faint hollow ring marks the commanded target
    when one is known. Axis inks follow the Position-tab series colors
    (α = slot 0, β = slot 1) so the entity color is consistent across the
    suite; the numeric readout sits in large mono beneath the pad —
    sized to be readable from across a control room. Values beyond
    ±full-scale clamp to the pad edge so the dot never escapes the frame.
    """

    RANGE = 20.0                          # ± degrees full-scale (matches axes)
    TEXT_H = 36.0                         # readout strip under the pad

    def __init__(self, parent=None):
        super().__init__(parent)
        self._alpha: Optional[float] = None
        self._beta: Optional[float] = None
        self._alpha_t: Optional[float] = None
        self._beta_t: Optional[float] = None
        self.setMinimumSize(190, 214)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)

    def set_values(self, alpha: Optional[float], beta: Optional[float],
                   alpha_t: Optional[float] = None,
                   beta_t: Optional[float] = None) -> None:
        self._alpha, self._beta = alpha, beta
        self._alpha_t, self._beta_t = alpha_t, beta_t
        self.update()

    @staticmethod
    def _fmt(v: Optional[float]) -> str:
        if v is None:
            return "—"
        return f"{v:+.1f}°".replace("-", "−")    # true minus sign

    def paintEvent(self, event) -> None:               # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        w, h = float(self.width()), float(self.height())
        text_h = self.TEXT_H
        s = min(w - 8.0, h - text_h - 8.0)
        if s < 24.0:
            p.end()
            return
        x0 = (w - s) / 2.0
        # centre the pad + readout block vertically in the widget
        y0 = max(3.0, (h - text_h - s) / 2.0)
        rng = self.RANGE
        cx, cy = x0 + s / 2.0, y0 + s / 2.0

        def mx(b: float) -> float:
            return x0 + (max(-rng, min(rng, b)) + rng) / (2 * rng) * s

        def my(a: float) -> float:
            return y0 + (rng - max(-rng, min(rng, a))) / (2 * rng) * s

        # pad background + border
        p.setPen(QPen(QColor(theme.BORDER), 1.0))
        p.setBrush(QBrush(QColor(theme.BG)))
        p.drawRoundedRect(QRectF(x0, y0, s, s), 6.0, 6.0)

        # faint 10° grid
        p.setPen(QPen(QColor(theme.GRID), 1.0))
        for d in (-10.0, 10.0):
            gx, gy = mx(d), my(d)
            p.drawLine(QPointF(gx, y0), QPointF(gx, y0 + s))
            p.drawLine(QPointF(x0, gy), QPointF(x0 + s, gy))

        # centre axes tinted with the α/β series colors
        a_col = QColor(theme.series_color(0)); a_col.setAlpha(150)
        b_col = QColor(theme.series_color(1)); b_col.setAlpha(150)
        p.setPen(QPen(a_col, 1.3))          # vertical = alpha
        p.drawLine(QPointF(cx, y0), QPointF(cx, y0 + s))
        p.setPen(QPen(b_col, 1.3))          # horizontal = beta
        p.drawLine(QPointF(x0, cy), QPointF(x0 + s, cy))

        # short degree ticks at ±10/±20 on both axes
        p.setPen(QPen(QColor(theme.AXIS), 1.0))
        for d in (-20.0, -10.0, 10.0, 20.0):
            tx, ty = mx(d), my(d)
            p.drawLine(QPointF(tx, cy - 4), QPointF(tx, cy + 4))
            p.drawLine(QPointF(cx - 4, ty), QPointF(cx + 4, ty))

        # axis letters + full-scale label (scale gently with the pad)
        lbl_pt = max(8, int(s * 0.055))
        p.setFont(QFont("Consolas", lbl_pt))
        fm = p.fontMetrics()
        p.setPen(QColor(theme.TEXT_DIM))
        p.drawText(QPointF(cx + 5, y0 + fm.ascent() + 3), "α")
        p.drawText(QPointF(x0 + s - fm.horizontalAdvance("β") - 6,
                           cy - 5), "β")
        p.drawText(QPointF(x0 + 6, y0 + s - 6), "±20°")

        # commanded-target ghost marker
        if self._alpha_t is not None and self._beta_t is not None:
            p.setPen(QPen(QColor(theme.TEXT_DIM), 1.4))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(mx(self._beta_t), my(self._alpha_t)),
                          6.5, 6.5)

        # live dot with a soft glow
        if self._alpha is not None and self._beta is not None:
            dx, dy = mx(self._beta), my(self._alpha)
            glow = QColor(theme.ACCENT_LIGHT); glow.setAlpha(60)
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(glow))
            p.drawEllipse(QPointF(dx, dy), 10.0, 10.0)
            p.setBrush(QBrush(QColor(theme.ACCENT_LIGHT)))
            p.setPen(QPen(QColor(theme.BG), 1.0))
            p.drawEllipse(QPointF(dx, dy), 5.0, 5.0)

        # readout: α (series 0) + β (series 1) — LARGE mono, centred
        # beneath the pad (readable from across the control room)
        val_pt = max(11, int(s * 0.085))
        p.setFont(QFont("Consolas", val_pt, QFont.Weight.DemiBold))
        a_txt = "α " + self._fmt(self._alpha)
        b_txt = "β " + self._fmt(self._beta)
        fm = p.fontMetrics()
        gap = max(14.0, s * 0.09)
        wa = float(fm.horizontalAdvance(a_txt))
        wb = float(fm.horizontalAdvance(b_txt))
        tx = (w - (wa + gap + wb)) / 2.0
        ty = y0 + s + (text_h + fm.ascent()) / 2.0 - 2.0
        p.setPen(QColor(theme.series_color(0)))
        p.drawText(QPointF(tx, ty), a_txt)
        p.setPen(QColor(theme.series_color(1)))
        p.drawText(QPointF(tx + wa + gap, ty), b_txt)
        p.end()


class TunnelDashboard(QWidget):
    """Gauge + tiles + status lights over the Mach/q/RPM strip charts."""

    def __init__(self, manager, parent=None):
        super().__init__(parent)
        theme.apply_pyqtgraph_theme()
        import pyqtgraph as pg
        self._pg = pg

        self.manager = manager
        self.active = False
        self._t0 = time.monotonic()
        self._hist: Dict[str, deque] = {}
        self._tunnel: Optional[SetpointDevice] = None
        self._positioner = None
        self._targets: Dict[str, Optional[float]] = {"alpha": None,
                                                     "beta": None}
        self._ok = self._import_widgets()
        self._build()
        self._discover()

        self._timer = QTimer(self)
        self._timer.setInterval(SAMPLE_MS)
        self._timer.timeout.connect(self._sample)
        self._timer.start()

    def _import_widgets(self) -> bool:
        try:
            from tunnel_plc.app.widgets import FanRotor, LedLamp, RpmGauge
            self._RpmGauge, self._FanRotor, self._LedLamp = (
                RpmGauge, FanRotor, LedLamp)
            return True
        except Exception:                              # noqa: BLE001
            self._RpmGauge = self._FanRotor = self._LedLamp = None
            return False

    # ── UI ───────────────────────────────────────────────────────────────
    def _build(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(6)
        BAND_H = 258                      # ONE height for every top-band box

        # fan gauge + rotor (reused device-app instruments). Min width is
        # sized so the gauge's outer tick labels ("300"/"1600") never clip
        # at the box edges: at this band height the labels reach ≈139 px
        # from the gauge centre.
        if self._ok:
            fan_box = QGroupBox("Fan")
            fg = QGridLayout(fan_box)
            fg.setContentsMargins(8, 4, 8, 6)
            self.rotor = self._FanRotor(diameter=40)
            fg.addWidget(self.rotor, 0, 0,
                         alignment=Qt.AlignmentFlag.AlignLeft |
                         Qt.AlignmentFlag.AlignTop)
            self.gauge = self._RpmGauge()
            # cap the gauge height so it stays HEIGHT-limited: the gauge
            # scales its dial off min(width, height*1.08) and paints the
            # outer tick labels ~13 px beyond the dial, so a width-limited
            # gauge always clips "300"/"1600" at its own edges. 206 px tall
            # → dial ≈ 222, labels fit inside the 272 px-min box.
            self.gauge.setMaximumHeight(206)
            fg.addWidget(self.gauge, 0, 0, 2, 2)
            fg.setRowStretch(1, 1)
            fg.setColumnStretch(1, 1)
            fan_box.setMinimumWidth(272)
            fan_box.setMaximumWidth(332)
            fan_box.setFixedHeight(BAND_H)
            top.addWidget(fan_box, 0)
            self._fan_box = fan_box
        else:
            self.rotor = self.gauge = None
            self._fan_box = None
            note = QLabel("tunnel_plc widgets unavailable")
            note.setObjectName("dim")
            top.addWidget(note, 0)

        # live α/β attitude crosshair (sourced from the positioner) — a
        # PROMINENT instrument next to the fan gauge, with a large mono
        # readout beneath the pad
        att_box = QGroupBox("Attitude α/β")
        att_box.setToolTip("Live sting attitude — β (horizontal) vs "
                           "α (vertical), read from the positioner")
        ag = QVBoxLayout(att_box)
        ag.setContentsMargins(8, 4, 8, 6)
        self._att = AttitudePad()
        ag.addWidget(self._att)
        att_box.setMinimumWidth(212)
        att_box.setMaximumWidth(256)
        att_box.setFixedHeight(BAND_H)
        top.addWidget(att_box, 0)
        self._att_box = att_box

        # stat tiles — 3 × 2 grid so the band's full height carries data:
        # charted quantities on top (accent strips key to their strip-chart
        # series colors: Mach = slot 0, q = slot 1, RPM = slot 2), ambient
        # + derived velocity below (neutral strips)
        tiles_wrap = QWidget()
        tiles_wrap.setFixedHeight(BAND_H)
        grid = QGridLayout(tiles_wrap)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(6)
        self.tiles: Dict[str, StatTile] = {}
        # captions kept SHORT so they never elide at the default window
        # width; the long-form description moves into the tooltip
        for i, (key, label, unit, fmt, accent, tip) in enumerate((
                ("mach", "Mach", "", "{:.3f}", theme.series_color(0),
                 "Isentropic Mach from Pdiff/Ptot/Temp (the one derived "
                 "chain shared with the Mach loop and recorder)"),
                ("q", "q (dynamic)", "psi", "{:.3f}", theme.series_color(1),
                 "Dynamic pressure from the same isentropic chain"),
                ("rpm", "Fan RPM / set", "", "{:.0f}", theme.series_color(2),
                 "Fan RPM readback / commanded setpoint"),
                ("ptot", "P total", "psia", "{:.2f}", "",
                 "Total pressure (DaqBook Ptot)"),
                ("temp", "T total", "°C", "{:.1f}", "",
                 "Total temperature (DaqBook Temp)"),
                ("vel", "Velocity", "m/s", "{:.1f}", "",
                 "Derived test-section velocity"))):
            tile = StatTile(label, unit, fmt,
                            value_pt=15 if key == "rpm" else 19,
                            accent=accent)
            tile.setToolTip(tip)
            self.tiles[key] = tile
            grid.addWidget(tile, i // 3, i % 3)
        for c in range(3):
            grid.setColumnStretch(c, 1)    # equal tile widths
        for r in range(2):
            grid.setRowStretch(r, 1)       # equal tile heights
        top.addWidget(tiles_wrap, 1)

        # VersaMax status lamps — glowing chip indicators (single column;
        # a 2-column grid starved the stat tiles at default width)
        lamp_box = QGroupBox("Tunnel status")
        lamp_box.setToolTip("VersaMax PLC status lights")
        lg = QVBoxLayout(lamp_box)
        lg.setContentsMargins(8, 4, 8, 6)
        lg.setSpacing(4)
        self.lamps: Dict[str, StatusLamp] = {}
        for attr, label, bad in _LAMPS:
            lamp = StatusLamp(label, bad_when_lit=bad)
            self.lamps[attr] = lamp
            lg.addWidget(lamp, 1)
        self.stale_lbl = QLabel("")
        self.stale_lbl.setStyleSheet(f"color: {theme.WARNING}; "
                                     "background: transparent;")
        lg.addWidget(self.stale_lbl, 0)
        lamp_box.setMinimumWidth(160)      # explicit — labels may clip
        lamp_box.setMaximumWidth(220)
        lamp_box.setFixedHeight(BAND_H)
        top.addWidget(lamp_box, 0)
        self._lamp_box = lamp_box

        top_wrap = QWidget()
        top_wrap.setLayout(top)
        top_wrap.setMaximumHeight(BAND_H + 16)
        # Explicit tiny minimum: the band's content minimum (~900 px) must
        # NEVER propagate up as the central widget's minimum — that is
        # what forced the window past the screen and painted the graph
        # pane over the Devices dock. The band keeps its normal preferred
        # size (so it still claims width at default window sizes) but can
        # compress/clip gracefully when the window is narrow.
        top_wrap.setMinimumWidth(1)
        root.addWidget(top_wrap, 0)

        # strip charts — Mach / q / RPM stacked, linked x (as before)
        pg = self._pg
        self._glw = pg.GraphicsLayoutWidget()
        self._p_mach = self._glw.addPlot(row=0, col=0)
        self._p_q = self._glw.addPlot(row=1, col=0)
        self._p_rpm = self._glw.addPlot(row=2, col=0)
        for p, label in ((self._p_mach, "Mach [-]"),
                         (self._p_q, "q [psi]"),
                         (self._p_rpm, "Fan RPM")):
            p.showGrid(x=True, y=True, alpha=0.25)
            p.setLabel("left", label)
            for side in ("left", "bottom"):
                # engineering units as-is — no "(x0.001)" SI mangling
                p.getAxis(side).enableAutoSIPrefix(False)
            # ONE fixed left-axis width for all three strips so their
            # plot rectangles align exactly regardless of tick-label
            # digits ("0.25" vs "0.55" vs "600" would otherwise stagger
            # the left edges). 56 px fits axis label + 4-digit ticks.
            p.getAxis("left").setWidth(56)
        self._p_rpm.setLabel("bottom", "t [s]")
        self._p_q.setXLink(self._p_mach)
        self._p_rpm.setXLink(self._p_mach)
        # width-1 non-AA pens: wider AA streaming pens hit Qt's slow path
        # stroker when repainting embedded (see strainbook app plots)
        self._c_mach = self._p_mach.plot(
            pen=pg.mkPen(theme.series_color(0), width=1), antialias=False)
        self._c_q = self._p_q.plot(
            pen=pg.mkPen(theme.series_color(1), width=1), antialias=False)
        self._c_rpm = self._p_rpm.plot(
            pen=pg.mkPen(theme.series_color(2), width=1), antialias=False)
        root.addWidget(self._glw, 1)

    def resizeEvent(self, event) -> None:              # noqa: N802
        """Adaptive top band: the stat-tile NUMBERS always win. When the
        dashboard gets narrow (both docks open on a small window), drop
        the status-light column first, then the fan gauge, then the α/β
        pad (last — it is the primary attitude instrument), instead of
        squeezing every tile value into unreadable clipped digits. The
        RPM tile and strip chart keep showing the fan numerically.
        Thresholds are tuned to the boxes' minimum widths so every tile
        keeps ≥ ~130 px before a neighbour is dropped."""
        super().resizeEvent(event)
        w = event.size().width()
        lamp_box = getattr(self, "_lamp_box", None)
        if lamp_box is not None:
            lamp_box.setVisible(w >= 1060)
        fan_box = getattr(self, "_fan_box", None)
        if fan_box is not None:
            fan_box.setVisible(w >= 880)
        att_box = getattr(self, "_att_box", None)
        if att_box is not None:
            att_box.setVisible(w >= 600)

    # ── discovery ────────────────────────────────────────────────────────
    def set_manager(self, manager) -> None:
        self.manager = manager
        self._hist.clear()
        self._t0 = time.monotonic()
        self._discover()

    def set_targets(self, alpha: Optional[float],
                    beta: Optional[float]) -> None:
        """Commanded α/β target for the attitude pad's ghost marker."""
        self._targets["alpha"] = alpha
        self._targets["beta"] = beta

    def _discover(self) -> None:
        dev = self.manager.by_role("tunnel")
        self._tunnel = dev if hasattr(dev, "snapshot") else None
        # positioner (crescent, ate, lswt_sting…) feeds the α/β pad;
        # positions() reports alpha/beta in deg for all attitude rigs
        self._positioner = getattr(self.manager, "positioner", None)

    # ── sampling ─────────────────────────────────────────────────────────
    def _push(self, key: str, t: float, v: float) -> None:
        self._hist.setdefault(key, deque(maxlen=HISTORY_N)).append((t, v))

    def _sample(self) -> None:
        if not self.active:
            return
        t = time.monotonic() - self._t0

        # tunnel conditions found BY CHANNEL NAME across the registry's
        # streaming devices (SWT: one DaqBook; LSWT: Pdiff from the NI
        # DAQ, Ptot/Temp from the Heise). Partial availability still
        # shows the raw Ptot/Temp tiles; Mach/q need all three.
        mach = q = ptot = temp = vel = None
        try:
            vals = read_tunnel_conditions(self.manager)
            ptot = vals.get("Ptot")
            temp = vals.get("Temp")
            if all(k in vals for k in TUNNEL_CONDITION_CHANNELS):
                st = tunnel_state(vals["Pdiff"], ptot, temp)
                if st.valid:
                    mach, q, vel = st.mach, st.q_psi, st.velocity_ms
                    self._push("mach", t, mach)
                    self._push("q", t, q)
        except Exception:                              # noqa: BLE001
            ptot = temp = None
        self.tiles["mach"].set_value(mach)
        self.tiles["q"].set_value(q)
        self.tiles["ptot"].set_value(ptot)
        self.tiles["temp"].set_value(temp)
        self.tiles["vel"].set_value(vel)

        rpm = rpm_set = None
        sp = self.manager.setpoint
        if sp is not None:
            try:
                rb = sp.readback()
                rpm = float(rb.get("rpm", 0.0))
                rpm_set = float(rb.get("rpm_set", 0.0))
                self._push("rpm", t, rpm)
            except Exception:                          # noqa: BLE001
                pass
        if rpm is None:
            self.tiles["rpm"].value.setText("—")
        else:
            self.tiles["rpm"].value.setText(
                f"{rpm:.0f} / {rpm_set:.0f}" if rpm_set is not None
                else f"{rpm:.0f}")

        if self._tunnel is not None:
            try:
                snap = self._tunnel.snapshot()
            except Exception:                          # noqa: BLE001
                snap = None
            if snap is not None:
                if self.gauge is not None:
                    rpm_max = float(getattr(self._tunnel.config, "rpm_max",
                                            0.0) or 1000.0)
                    self.gauge.set_state(snap.actual_rpm, snap.rpm_set,
                                         rpm_max)
                    self.rotor.set_state(snap.actual_rpm,
                                         snap.fan_running)
                for attr, lamp in self.lamps.items():
                    lamp.set_lit(bool(getattr(snap, attr, False)))
                self.stale_lbl.setText("⚠ snapshot stale"
                                       if snap.stale else "")

        # live α/β attitude pad — positions() gives alpha/beta in deg for
        # every attitude rig (crescent, ate, lswt_sting)
        if self._positioner is not None:
            try:
                pp = self._positioner.positions()
                self._att.set_values(pp.get("alpha"), pp.get("beta"),
                                     self._targets.get("alpha"),
                                     self._targets.get("beta"))
            except Exception:                          # noqa: BLE001
                pass
        self._redraw()

    def _redraw(self) -> None:
        def data(key):
            h = self._hist.get(key)
            if not h:
                return [], []
            ts, vs = zip(*h)
            return list(ts), list(vs)

        self._c_mach.setData(*data("mach"))
        self._c_q.setData(*data("q"))
        self._c_rpm.setData(*data("rpm"))

    def shutdown(self) -> None:
        self._timer.stop()
