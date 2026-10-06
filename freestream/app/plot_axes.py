"""One axis engine for every live plot: Auto / Manual per axis, smooth or
interval auto-scaling, and the shared display low-pass filter.

pyqtgraph's own context menu (X axis / Y axis submenus, mouse mode,
transforms, downsampling, ...) and its free-running auto-range are
switched off on every plot the engine drives. In their place:

* :class:`PlotAxesEngine` owns the ranges of a GROUP of plots that share
  an x axis (the x-linked Tunnel strips, the Balance bridge + excitation
  pair, ...). Each axis is ``auto`` or ``manual``:

  - manual: the range the operator typed (or dragged / zoomed to with
    the mouse - that switches the axis to manual and fills the fields);
  - auto: the data range plus a margin, reached SMOOTHLY - the view
    glides toward it (fast when the data leaves the view, slow when it
    could shrink) so the axis never jumps. The update setting can
    instead hold each target for 1-10 s before gliding to the next.
  - a TIME x axis in auto follows the newest sample over a chosen window.

* :class:`PlotAxesBar` is the compact control strip shown above a group:
  X and Y Auto/Manual with min/max entry, the update mode, the display
  low-pass filter and an "auto-scale all" button.

* :func:`display` is the shared visualization settings hub; its low-pass
  cutoff (``config.display_lpf_hz``) filters every live trace and the
  Forces readout. Recorded data is never filtered.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np
from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QDoubleValidator
from PyQt6.QtWidgets import (QButtonGroup, QComboBox, QHBoxLayout, QLabel,
                             QLineEdit, QMenu, QPushButton, QWidget)

#: display low-pass choices (Hz; 0 = off)
LPF_CHOICES = (0.0, 0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0)
#: auto-scale update modes (s; 0 = continuous smooth)
UPDATE_CHOICES = (0.0, 1.0, 2.0, 5.0, 10.0)
#: time-axis windows in auto (s; 0 = all history)
WINDOW_CHOICES = (10.0, 30.0, 60.0, 120.0, 0.0)

#: smoothing time constants (s)
TAU_EXPAND = 0.12          # data left the view: catch up quickly
TAU_CONTRACT = 1.5         # view could shrink: drift in slowly
TAU_INTERVAL = 0.25        # interval mode: glide to each held target
TAU_SCROLL = 0.20          # time axis following the newest sample
MARGIN = 0.08              # auto range margin (fraction of the span)
TICK_MS = 33


# ════════════════════════════════════════════════════════════════════════
#  Shared display settings (the visualization low-pass)
# ════════════════════════════════════════════════════════════════════════

class _Signals(QObject):
    lpf_changed = pyqtSignal(float)


class DisplaySettings:
    """Process-wide visualization settings. Plain object (outlives any one
    QApplication in the test suite); its Qt signals are recreated on
    demand."""

    def __init__(self):
        self._signals = _Signals()
        self._config = None
        self._lpf = 0.0

    @property
    def lpf_changed(self):
        try:
            from PyQt6 import sip
            if sip.isdeleted(self._signals):
                self._signals = _Signals()
        except ImportError:                            # pragma: no cover
            pass
        return self._signals.lpf_changed

    def bind(self, config) -> None:
        """Back the cutoff with ``config.display_lpf_hz`` (persisted with
        the session config / Set as Defaults)."""
        self._config = config
        self._lpf = float(getattr(config, "display_lpf_hz", 0.0) or 0.0)

    @property
    def lpf_hz(self) -> float:
        if self._config is not None:
            self._lpf = float(
                getattr(self._config, "display_lpf_hz", 0.0) or 0.0)
        return self._lpf

    def set_lpf_hz(self, hz: float) -> None:
        hz = max(float(hz), 0.0)
        if self._config is not None:
            self._config.display_lpf_hz = hz
        if hz != self._lpf:
            self._lpf = hz
            self.lpf_changed.emit(hz)


_DISPLAY: Optional[DisplaySettings] = None


def display() -> DisplaySettings:
    global _DISPLAY
    if _DISPLAY is None:
        _DISPLAY = DisplaySettings()
    return _DISPLAY


def lowpass(x: np.ndarray, rate_hz: float, cutoff_hz: float) -> np.ndarray:
    """Monitor-grade low-pass: moving-average FIR sized to
    ``rate/cutoff`` (first null ~cutoff), edge-hold padded so the trace
    never rolls off toward zero at the window ends. Display only."""
    x = np.asarray(x, dtype=float)
    if cutoff_hz <= 0 or rate_hz <= 0:
        return x
    n = int(round(rate_hz / cutoff_hz))
    if n < 2 or x.size < n:
        return x
    xp = np.pad(x, (n // 2, n - 1 - n // 2), mode="edge")
    return np.convolve(xp, np.full(n, 1.0 / n), mode="valid")


def lowpass_trace(t: Sequence[float], v: Sequence[float],
                  cutoff_hz: Optional[float] = None) -> np.ndarray:
    """Low-pass a time-stamped history (rate estimated from the stamps)
    at the shared display cutoff (or ``cutoff_hz``). NaN gaps pass."""
    v = np.asarray(v, dtype=float)
    cutoff = display().lpf_hz if cutoff_hz is None else cutoff_hz
    if cutoff <= 0 or v.size < 3:
        return v
    t = np.asarray(t, dtype=float)
    dt = np.diff(t)
    dt = dt[np.isfinite(dt) & (dt > 0)]
    if not dt.size:
        return v
    finite = np.isfinite(v)
    if not finite.all():
        out = v.copy()
        out[finite] = lowpass(v[finite], 1.0 / float(np.median(dt)), cutoff)
        return out
    return lowpass(v, 1.0 / float(np.median(dt)), cutoff)


# ════════════════════════════════════════════════════════════════════════
#  Engine
# ════════════════════════════════════════════════════════════════════════

@dataclass
class AxisState:
    mode: str = "auto"                     # 'auto' | 'manual'
    lo: Optional[float] = None
    hi: Optional[float] = None


def _ease(cur: float, target: float, dt: float, tau: float) -> float:
    return cur + (target - cur) * (1.0 - math.exp(-dt / max(tau, 1e-3)))


def _padded(lo: float, hi: float) -> Tuple[float, float]:
    span = hi - lo
    if not math.isfinite(span):
        return lo, hi
    if span <= 0:
        # a flat trace (e.g. a stopped fan at 0 RPM): a readable band,
        # not +-0.0005 of tick-label noise
        pad = abs(lo) * 0.05 if lo != 0 else 1.0
        return lo - pad, hi + pad
    return lo - MARGIN * span, hi + MARGIN * span


class PlotAxesEngine(QObject):
    """Ranges for a group of plots sharing one x axis (see module doc)."""

    changed = pyqtSignal()

    def __init__(self, plots: Sequence, names: Sequence[str] = (),
                 time_x: bool = True, window_s: float = 60.0,
                 on_clear: Optional[Callable[[], None]] = None,
                 parent: Optional[QObject] = None):
        super().__init__(parent)
        self.plots = list(plots)
        self.names = list(names) or [f"plot {i + 1}"
                                     for i in range(len(self.plots))]
        self.time_x = time_x
        self.window_s = float(window_s)
        self.update_s = 0.0
        self.on_clear = on_clear
        self.x = AxisState()
        self.y: Dict[int, AxisState] = {i: AxisState()
                                        for i in range(len(self.plots))}
        self._y_target: Dict[int, Tuple[float, float]] = {}
        self._y_held_at: Dict[int, float] = {}
        self._x_target: Optional[Tuple[float, float]] = None
        self._y_seen: set = set()
        self._applying = False
        self._last = time.monotonic()
        for i, pi in enumerate(self.plots):
            vb = pi.getViewBox()
            vb.disableAutoRange()
            vb.setMenuEnabled(False)
            pi.setMenuEnabled(False)
            pi.hideButtons()
            vb.sigRangeChangedManually.connect(
                lambda mask, i=i: self._user_moved(i, mask))
        self._scenes = []
        for pi in self.plots:
            scene = pi.scene()
            if scene is not None and scene not in self._scenes:
                self._scenes.append(scene)
                scene.sigMouseClicked.connect(self._scene_clicked)
        self._timer = QTimer(self)
        self._timer.setInterval(TICK_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ── public API ──────────────────────────────────────────────────────
    def set_x(self, mode: str, lo: Optional[float] = None,
              hi: Optional[float] = None) -> None:
        if mode == "manual":
            lo, hi = self._fill(lo, hi, self.view(0)[0])
            self.x = AxisState("manual", lo, hi)
            self._apply_x(lo, hi)
        else:
            self.x = AxisState("auto")
        self.changed.emit()

    def set_y(self, index: Optional[int], mode: str,
              lo: Optional[float] = None,
              hi: Optional[float] = None) -> None:
        """``index`` None = every plot of the group."""
        targets = range(len(self.plots)) if index is None else [index]
        for i in targets:
            if mode == "manual":
                a, b = self._fill(lo, hi, self.view(i)[1])
                self.y[i] = AxisState("manual", a, b)
                self._apply_y(i, a, b)
            else:
                self.y[i] = AxisState("auto")
                self._y_held_at.pop(i, None)
        self.changed.emit()

    def set_update(self, seconds: float) -> None:
        self.update_s = max(float(seconds), 0.0)
        self._y_held_at.clear()
        self.changed.emit()

    def set_window(self, seconds: float) -> None:
        self.window_s = max(float(seconds), 0.0)
        self.changed.emit()

    def auto_all(self) -> None:
        self.x = AxisState("auto")
        for i in self.y:
            self.y[i] = AxisState("auto")
        self._y_held_at.clear()
        self.changed.emit()

    def view(self, index: int) -> Tuple[Tuple[float, float],
                                        Tuple[float, float]]:
        (x0, x1), (y0, y1) = self.plots[index].getViewBox().viewRange()
        return (x0, x1), (y0, y1)

    def stop(self) -> None:
        self._timer.stop()

    # ── interaction ─────────────────────────────────────────────────────
    def _user_moved(self, index: int, mask) -> None:
        """A mouse drag / wheel zoom: the moved axis becomes manual at
        exactly what the operator is looking at."""
        if self._applying:
            return
        (x0, x1), (y0, y1) = self.view(index)
        mx = mask[0] if isinstance(mask, (list, tuple)) else True
        my = mask[1] if isinstance(mask, (list, tuple)) else True
        if mx:
            self.x = AxisState("manual", x0, x1)
        if my:
            self.y[index] = AxisState("manual", y0, y1)
        self.changed.emit()

    def _scene_clicked(self, ev) -> None:
        if ev.button() != Qt.MouseButton.RightButton:
            return
        ev.accept()
        menu = QMenu()
        menu.addAction("Auto-scale all axes").triggered.connect(
            self.auto_all)
        menu.addAction("Auto-scale Y only").triggered.connect(
            lambda: self.set_y(None, "auto"))
        if self.on_clear is not None:
            menu.addSeparator()
            menu.addAction("Clear plot").triggered.connect(self.on_clear)
        pos = ev.screenPos()
        menu.exec(pos.toPoint())

    # ── ranges ──────────────────────────────────────────────────────────
    @staticmethod
    def _fill(lo, hi, view) -> Tuple[float, float]:
        lo = view[0] if lo is None else float(lo)
        hi = view[1] if hi is None else float(hi)
        if hi < lo:
            lo, hi = hi, lo
        if hi == lo:
            hi = lo + 1.0
        return lo, hi

    def _apply_x(self, lo: float, hi: float) -> None:
        self._applying = True
        try:
            # one call moves the whole group: the other plots are x-linked
            # (or, if not, get the same range explicitly)
            for pi in self.plots:
                pi.getViewBox().setXRange(lo, hi, padding=0)
        finally:
            self._applying = False

    def _apply_y(self, i: int, lo: float, hi: float) -> None:
        self._applying = True
        try:
            self.plots[i].getViewBox().setYRange(lo, hi, padding=0)
        finally:
            self._applying = False

    def _curves(self, i: int):
        for item in self.plots[i].listDataItems():
            try:
                x, y = item.getData()
            except Exception:                          # noqa: BLE001
                continue
            if x is None or y is None or not len(x):
                continue
            yield np.asarray(x, dtype=float), np.asarray(y, dtype=float)

    def _x_bounds(self) -> Optional[Tuple[float, float]]:
        lo, hi = math.inf, -math.inf
        for i in range(len(self.plots)):
            for x, _y in self._curves(i):
                fin = x[np.isfinite(x)]
                if fin.size:
                    lo, hi = min(lo, fin.min()), max(hi, fin.max())
        return None if hi < lo else (lo, hi)

    def _y_bounds(self, i: int, x_range) -> Optional[Tuple[float, float]]:
        lo, hi = math.inf, -math.inf
        for x, y in self._curves(i):
            sel = np.isfinite(y) & np.isfinite(x)
            if x_range is not None:
                sel &= (x >= x_range[0]) & (x <= x_range[1])
            if sel.any():
                lo, hi = min(lo, y[sel].min()), max(hi, y[sel].max())
        return None if hi < lo else (lo, hi)

    def _tick(self, now: Optional[float] = None,
              force: bool = False) -> None:
        """One animation step (QTimer-driven; ``now`` / ``force`` let tests
        step it deterministically, even off-screen)."""
        now = time.monotonic() if now is None else now
        dt = min(max(now - self._last, 0.0), 0.25)
        self._last = now
        if not self.plots or not (
                force or self.plots[0].getViewBox().isVisible()):
            return

        # ── x ──
        if self.x.mode == "auto":
            bounds = self._x_bounds()
            if bounds is not None:
                if self.time_x:
                    hi = bounds[1]
                    # a history shorter than the window fills the plot
                    # instead of hugging its right edge
                    lo = max(hi - self.window_s, bounds[0]) \
                        if self.window_s > 0 else bounds[0]
                    if hi - lo <= 0:
                        lo = hi - 1.0
                    target = (lo, hi)
                    tau = TAU_SCROLL
                else:
                    target = _padded(*bounds)
                    tau = TAU_INTERVAL
                (c0, c1), _ = self.view(0)
                if self._x_target is None or not all(
                        map(math.isfinite, (c0, c1))):
                    new = target
                else:
                    new = (_ease(c0, target[0], dt, tau),
                           _ease(c1, target[1], dt, tau))
                self._x_target = target
                if abs(new[0] - c0) + abs(new[1] - c1) > 1e-9:
                    self._apply_x(*new)
        x_range = self.view(0)[0]

        # ── y, per plot ──
        for i in range(len(self.plots)):
            st = self.y[i]
            if st.mode != "auto":
                continue
            held = self._y_held_at.get(i)
            if self.update_s <= 0 or held is None \
                    or now - held >= self.update_s:
                bounds = self._y_bounds(i, x_range)
                if bounds is None:
                    continue
                self._y_target[i] = _padded(*bounds)
                self._y_held_at[i] = now
            target = self._y_target.get(i)
            if target is None:
                continue
            _, (c0, c1) = self.view(i)
            if i not in self._y_seen or not (
                    math.isfinite(c0) and math.isfinite(c1)):
                new = target                 # first fit: no glide in
                self._y_seen.add(i)
            elif self.update_s > 0:
                new = (_ease(c0, target[0], dt, TAU_INTERVAL),
                       _ease(c1, target[1], dt, TAU_INTERVAL))
            else:
                # expand fast when data leaves the view, contract slowly
                t0 = TAU_EXPAND if target[0] < c0 else TAU_CONTRACT
                t1 = TAU_EXPAND if target[1] > c1 else TAU_CONTRACT
                new = (_ease(c0, target[0], dt, t0),
                       _ease(c1, target[1], dt, t1))
            if abs(new[0] - c0) + abs(new[1] - c1) > 1e-12:
                self._apply_y(i, *new)


# ════════════════════════════════════════════════════════════════════════
#  Control strip
# ════════════════════════════════════════════════════════════════════════

def _fmt(v: float) -> str:
    if v == 0 or not math.isfinite(v):
        return "0"
    mag = abs(v)
    if 1e-3 <= mag < 1e5:
        return f"{v:.6g}"
    return f"{v:.4e}"


class _ModeToggle(QWidget):
    """Two-segment Auto | Manual switch."""

    toggled = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        self._group = QButtonGroup(self)
        self.buttons: Dict[str, QPushButton] = {}
        for i, (key, text) in enumerate((("auto", "Auto"),
                                         ("manual", "Manual"))):
            b = QPushButton(text)
            b.setCheckable(True)
            b.setObjectName("segmentLeft" if i == 0 else "segmentRight")
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            # never clipped to "Autc" / "lanu" when the strip is tight
            from PyQt6.QtGui import QFont, QFontMetrics
            b.ensurePolished()               # measure the STYLED font
            bold = QFont(b.font())
            bold.setBold(True)               # the checked segment is bold
            b.setMinimumWidth(
                QFontMetrics(bold).horizontalAdvance(text) + 30)
            self._group.addButton(b)
            self.buttons[key] = b
            lay.addWidget(b)
            b.clicked.connect(lambda _c=False, k=key: self.toggled.emit(k))
        self.buttons["auto"].setChecked(True)

    def set_mode(self, mode: str) -> None:
        b = self.buttons.get(mode)
        if b is not None and not b.isChecked():
            b.setChecked(True)

    def mode(self) -> str:
        return "manual" if self.buttons["manual"].isChecked() else "auto"


class PlotAxesBar(QWidget):
    """Compact control strip for one :class:`PlotAxesEngine`."""

    def __init__(self, engine: PlotAxesEngine, parent=None):
        super().__init__(parent)
        self.setObjectName("plotAxesBar")
        # never dictate the page's minimum width (the Tunnel dashboard
        # drops whole panels as it narrows); the strip clips instead
        from PyQt6.QtWidgets import QSizePolicy
        self.setSizePolicy(QSizePolicy.Policy.Ignored,
                           QSizePolicy.Policy.Fixed)
        self.setMinimumWidth(1)
        self.engine = engine
        # two groups - axes (X, Y) and options (update, LPF, auto all) -
        # side by side when there is room, stacked when there is not
        from PyQt6.QtWidgets import QBoxLayout
        self._outer = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._outer.setContentsMargins(4, 2, 4, 2)
        self._outer.setSpacing(4)
        self._axes_row = QWidget()
        self._opts_row = QWidget()
        for row in (self._axes_row, self._opts_row):
            # scoped to the row itself: an unscoped rule would cascade
            # into the buttons and erase the checked segment's fill
            row.setObjectName("plotAxesRow")
            row.setStyleSheet(
                "QWidget#plotAxesRow { background: transparent; }")
        lay = QHBoxLayout(self._axes_row)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(5)
        opts = QHBoxLayout(self._opts_row)
        opts.setContentsMargins(0, 0, 0, 0)
        opts.setSpacing(5)
        self._outer.addWidget(self._axes_row)
        self._outer.addWidget(self._opts_row, 1)

        def field(tip: str) -> QLineEdit:
            e = QLineEdit()
            e.setValidator(QDoubleValidator())
            e.setFixedWidth(78)
            e.setPlaceholderText(tip)
            e.setToolTip(tip)
            return e

        # ── X ──
        lay.addWidget(self._caption("X"))
        self.x_mode = _ModeToggle()
        self.x_mode.setToolTip(
            "Auto: follow the newest data (time axis) or fit the data; "
            "Manual: the range below. Dragging or zooming the plot "
            "switches the axis to Manual.")
        self.x_mode.toggled.connect(self._x_mode)
        lay.addWidget(self.x_mode)
        self.x_lo, self.x_hi = field("x min"), field("x max")
        for e in (self.x_lo, self.x_hi):
            e.editingFinished.connect(self._x_manual)
            lay.addWidget(e)
        self.window = QComboBox()
        for s in WINDOW_CHOICES:
            self.window.addItem("all" if s == 0 else f"{s:g} s", s)
        self.window.setToolTip("Time span shown while X is Auto")
        self.window.setCurrentIndex(
            max(0, list(WINDOW_CHOICES).index(engine.window_s))
            if engine.window_s in WINDOW_CHOICES else 2)
        self.window.currentIndexChanged.connect(
            lambda _i: engine.set_window(self.window.currentData()))
        self.window.setVisible(engine.time_x)
        lay.addWidget(self.window)
        lay.addSpacing(4)

        # ── Y ──
        lay.addWidget(self._caption("Y"))
        self.y_target = QComboBox()
        self.y_target.addItem("all", None)
        for i, name in enumerate(engine.names):
            self.y_target.addItem(name, i)
        self.y_target.setToolTip("Which plot's Y axis the controls set")
        self.y_target.setVisible(len(engine.plots) > 1)
        self.y_target.currentIndexChanged.connect(lambda _i: self.refresh())
        lay.addWidget(self.y_target)
        self.y_mode = _ModeToggle()
        self.y_mode.setToolTip(
            "Auto: fit the visible data, smoothly; Manual: the range "
            "below")
        self.y_mode.toggled.connect(self._y_mode)
        lay.addWidget(self.y_mode)
        self.y_lo, self.y_hi = field("y min"), field("y max")
        for e in (self.y_lo, self.y_hi):
            e.editingFinished.connect(self._y_manual)
            lay.addWidget(e)
        lay.addStretch(1)

        # ── update + LPF ──
        lay = opts
        lay.addWidget(self._caption("Update"))
        self.update_mode = QComboBox()
        for s in UPDATE_CHOICES:
            self.update_mode.addItem("smooth" if s == 0 else f"every {s:g} s",
                                     s)
        self.update_mode.setToolTip(
            "smooth: the auto range glides continuously (expands quickly, "
            "contracts slowly) so it never jumps; every N s: hold each "
            "auto range N seconds, then glide to the next")
        self.update_mode.currentIndexChanged.connect(
            lambda _i: engine.set_update(self.update_mode.currentData()))
        lay.addWidget(self.update_mode)
        lay.addWidget(self._caption("LPF"))
        self.lpf = QComboBox()
        for hz in LPF_CHOICES:
            self.lpf.addItem("off" if hz == 0 else f"{hz:g} Hz", hz)
        self.lpf.setToolTip(
            "Low-pass filter for what is DISPLAYED (traces and the Forces "
            "readout) - shared by every plot. Recorded data is never "
            "filtered.")
        self._sync_lpf(display().lpf_hz)
        self.lpf.currentIndexChanged.connect(
            lambda _i: display().set_lpf_hz(self.lpf.currentData()))
        display().lpf_changed.connect(self._sync_lpf)
        lay.addWidget(self.lpf)
        lay.addStretch(1)
        auto = QPushButton("↺ Auto all")
        auto.setToolTip("Every axis of these plots back to Auto")
        auto.clicked.connect(engine.auto_all)
        lay.addWidget(auto)

        engine.changed.connect(self.refresh)
        self.refresh()

    def resizeEvent(self, event) -> None:              # noqa: N802
        """One row when both groups fit at their natural width, two rows
        (options under axes) when they do not - nothing gets clipped."""
        from PyQt6.QtWidgets import QBoxLayout
        need = (self._axes_row.sizeHint().width()
                + self._opts_row.sizeHint().width() + 16)
        direction = (QBoxLayout.Direction.LeftToRight
                     if event.size().width() >= need
                     else QBoxLayout.Direction.TopToBottom)
        if self._outer.direction() != direction:
            self._outer.setDirection(direction)
            self.updateGeometry()
        super().resizeEvent(event)

    @staticmethod
    def _caption(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("dim")
        return lbl

    def _sync_lpf(self, hz: float) -> None:
        try:
            choices = [self.lpf.itemData(i) for i in range(self.lpf.count())]
            idx = min(range(len(choices)),
                      key=lambda k: abs(choices[k] - hz))
            if self.lpf.currentIndex() != idx:
                self.lpf.blockSignals(True)
                self.lpf.setCurrentIndex(idx)
                self.lpf.blockSignals(False)
        except RuntimeError:                           # widget deleted
            pass

    def _y_index(self) -> Optional[int]:
        return self.y_target.currentData()

    # ── handlers ────────────────────────────────────────────────────────
    def _x_mode(self, mode: str) -> None:
        self.engine.set_x(mode)

    def _y_mode(self, mode: str) -> None:
        self.engine.set_y(self._y_index(), mode)

    def _x_manual(self) -> None:
        lo, hi = self._read(self.x_lo, self.x_hi)
        if lo is not None or hi is not None:
            self.engine.set_x("manual", lo, hi)

    def _y_manual(self) -> None:
        lo, hi = self._read(self.y_lo, self.y_hi)
        if lo is not None or hi is not None:
            self.engine.set_y(self._y_index(), "manual", lo, hi)

    @staticmethod
    def _read(lo_edit: QLineEdit, hi_edit: QLineEdit):
        def val(e):
            try:
                return float(e.text())
            except ValueError:
                return None
        return val(lo_edit), val(hi_edit)

    def refresh(self) -> None:
        """Mirror the engine state into the controls."""
        try:
            eng = self.engine
            self.x_mode.set_mode(eng.x.mode)
            manual_x = eng.x.mode == "manual"
            # range fields only where they apply: keeps the strip compact
            for e in (self.x_lo, self.x_hi):
                e.setEnabled(manual_x)
                e.setVisible(manual_x)
            self.window.setEnabled(not manual_x)
            self.window.setVisible(eng.time_x and not manual_x)
            if manual_x:
                self.x_lo.setText(_fmt(eng.x.lo))
                self.x_hi.setText(_fmt(eng.x.hi))
            idx = self._y_index()
            states = [eng.y[i] for i in
                      (range(len(eng.plots)) if idx is None else [idx])]
            mode = "manual" if all(s.mode == "manual" for s in states) \
                else "auto"
            self.y_mode.set_mode(mode)
            for e in (self.y_lo, self.y_hi):
                e.setEnabled(mode == "manual")
                e.setVisible(mode == "manual")
            if mode == "manual" and len(states) == 1:
                self.y_lo.setText(_fmt(states[0].lo))
                self.y_hi.setText(_fmt(states[0].hi))
            elif mode != "manual":
                self.y_lo.clear()
                self.y_hi.clear()
        except RuntimeError:                           # widget deleted
            pass
