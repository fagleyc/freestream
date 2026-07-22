"""Live Forces monitor — .vol calibration → streaming lift/drag + max checks.

DISPLAY ONLY (team directive: coefficients/forces are never persisted; the
recorder stores raw volts). Each UI tick this pulls a non-consuming window
of raw balance bridge-volts (:meth:`StrainbookAdapter.raw_tail` — it must
NOT steal samples from the recorder), the current alpha/beta from the
positioner and q from the DaqBook, and reduces them through
:mod:`freestream.aero` into wind-axis Lift/Drag/Side + moments.

Per-element loads are compared against the balance's rated maxima (from the
``.vol``); amber ≥ warn threshold, red ≥ 100 %. When any element exceeds
100 % this panel raises ``overstress`` and contributes a blocker so
Freestream refuses to record an overloaded balance (§6.2).

For an external balance (Mode 2 / ATE) that already streams resolved loads
under their true names (Lift/Drag/Side/Pitch/Yaw/Roll), those are shown
directly — no .vol needed (the OGI owns the balance's own calibration) —
and the SAME load bars run against the adapter's ``load_limits`` (rated
maxima from the device config): utilization = |load|/max where a max is
known, the raw load value where none is. Overstress raises the identical
alarm banner + record blocker as the calibrated path.
"""

from __future__ import annotations

import time
from collections import deque
from pathlib import Path
from typing import Deque, Dict, Optional, Tuple

import numpy as np
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtGui import QColor, QPainter, QPen
from PyQt6.QtWidgets import (QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                             QLabel, QPushButton, QSizePolicy, QVBoxLayout,
                             QWidget)

from .. import theme
from ..aero import Geometry, balance_summary, compute_aero, load_balance_cal
from ..derived import tunnel_state
from ..hal import Streaming

SAMPLE_MS = 200
WINDOW_S = 1.0
#: rolling window for the peak-hold marker on the load bars
PEAK_HOLD_S = 30.0


class LoadBar(QWidget):
    """Element-load bar (0–120 % of rated max): filled fraction = live
    utilization, bright marker line = rolling peak over ``PEAK_HOLD_S``
    (reset on tare), thin tick at the 100 % rated limit. Expands to fill
    the panel's free vertical space."""

    SPAN = 1.2                       # full bar width = 120 %

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self._u: Optional[float] = None
        self._peak: Optional[float] = None
        self._color = theme.SUCCESS

    def set_load(self, u: Optional[float], peak: Optional[float],
                 color: str) -> None:
        if (u, peak, color) != (self._u, self._peak, self._color):
            self._u, self._peak, self._color = u, peak, color
            self.update()

    def paintEvent(self, _ev) -> None:                 # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = self.rect().adjusted(0, 0, -1, -1)
        p.setPen(QPen(QColor(theme.BORDER)))
        p.setBrush(QColor(theme.BG_LIGHTER))
        p.drawRoundedRect(r, 5, 5)
        w, h = r.width(), r.height()

        def x_at(u: float) -> int:
            return 1 + int((w - 2) * max(0.0, min(u, self.SPAN)) / self.SPAN)

        if self._u is not None and self._u > 0:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QColor(self._color))
            p.drawRoundedRect(1, 1, x_at(self._u) - 1, h - 1, 4, 4)
        # 100 % rated-limit tick
        tick = QColor(theme.TEXT_DIM)
        tick.setAlpha(140)
        p.setPen(QPen(tick, 1))
        x100 = x_at(1.0)
        p.drawLine(x100, 1, x100, h)
        # rolling-peak marker
        if self._peak is not None and self._peak > 0:
            xp = x_at(self._peak)
            p.setPen(QPen(QColor(theme.ERROR if self._peak >= 1.0
                                 else theme.TEXT), 2))
            p.drawLine(xp, 1, xp, h)
        p.end()
#: wind-axis tiles (attr on AeroResult.means, label, unit)
_TILES = [("Lift", "lb"), ("Drag", "lb"), ("Side", "lb"),
          ("Roll", "in·lb"), ("Pitch", "in·lb"), ("Yaw", "in·lb")]
#: load-bar order for a resolved-load (external) balance — the six REAL
#: channel names the ATE adapter streams
_RESOLVED_ORDER = ("Lift", "Drag", "Side", "Pitch", "Yaw", "Roll")


class _Tile(QFrame):
    def __init__(self, name: str, unit: str, color: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"QFrame {{ background-color: {theme.BG_LIGHT}; "
            f"border: 1px solid {theme.BORDER}; border-radius: 6px; }}"
            f"QLabel {{ border: none; background: transparent; }}")
        # explicit minimum so the 16pt value text never dictates the
        # panel's (and the central widget's) minimum width
        self.setMinimumWidth(88)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(1)
        head = QHBoxLayout()
        chip = QLabel()
        chip.setFixedSize(9, 9)
        chip.setStyleSheet(f"background-color: {color}; border-radius: 4px;")
        head.addWidget(chip)
        t = QLabel(name)
        t.setStyleSheet(f"color: {theme.TEXT_DIM}; font-weight: bold;")
        head.addWidget(t)
        head.addStretch(1)
        lay.addLayout(head)
        self.value = QLabel("—")
        self.value.setStyleSheet("font-family: Consolas, monospace; "
                                 f"font-size: 16pt; color: {theme.TEXT};")
        lay.addWidget(self.value)
        u = QLabel(unit)
        u.setObjectName("dim")
        lay.addWidget(u)

    def set_value(self, v: Optional[float]):
        if v is None:
            self.value.setText("—")
            return
        mag = abs(v)
        dec = 4 if mag < 1 else (3 if mag < 10 else 2)
        self.value.setText(f"{v:+,.{dec}f}")


class ForcesPanel(QWidget):
    """Stream lift/drag through the balance .vol, monitor element loads vs
    maxima. PURE LIVE READOUT: the .vol path, fit type and balance layout
    are edited ONLY in the StrainBook device panel's Forces tab (the
    device driver owns them, persisted with the device config); this page
    INHERITS all three from the balance adapter every tick and never
    edits them. Its "Balance device…" button asks the main window to open
    that one canonical editor."""

    #: ask the main window to open the balance device's config dialog
    #: (the StrainBook panel — the single .vol/fit/layout editor)
    configureBalanceRequested = pyqtSignal()

    def __init__(self, manager, config, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.config = config
        self.cal = None
        self.overstress = False
        self._balance: Optional[Streaming] = None
        self._daq: Optional[Streaming] = None
        # inherited state (device-owned; mirrored into the shared config
        # so the recorder metadata and session persistence stay correct)
        self._layout = config.balance_config or "Force"
        self._loaded_vol = config.vol_path or ""
        self._loaded_fit = config.cal_type or "Linear"
        # rolling peak-hold per element (marker on the load bars);
        # cleared whenever the balance's zero_count (tare) changes
        self._peak_hist: Dict[str, Deque[Tuple[float, float]]] = {}
        self._last_zero_count: Optional[int] = None
        self._build()
        self._discover()
        if config.vol_path:
            self.load_vol(config.vol_path)

        from PyQt6.QtCore import QTimer
        self._timer = QTimer(self)
        self._timer.setInterval(SAMPLE_MS)
        self._timer.timeout.connect(self._sample)
        self._timer.start()
        self.active = False

    # ── UI ───────────────────────────────────────────────────────────────
    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Balance .vol"))
        self.vol_lbl = QLabel("—")
        self.vol_lbl.setStyleSheet("font-family: Consolas, monospace;")
        bar.addWidget(self.vol_lbl)
        bar.addWidget(QLabel("Fit"))
        self.fit_lbl = QLabel(self._loaded_fit)
        self.fit_lbl.setStyleSheet("font-family: Consolas, monospace;")
        bar.addWidget(self.fit_lbl)
        bar.addWidget(QLabel("Layout"))
        self.layout_lbl = QLabel(self._layout)
        self.layout_lbl.setStyleSheet("font-family: Consolas, monospace;")
        bar.addWidget(self.layout_lbl)
        self.change_btn = QPushButton("Balance device…")
        self.change_btn.setToolTip(
            "The .vol calibration, fit type and balance layout are set in "
            "the StrainBook device panel (Forces tab) — this opens it. "
            "This page inherits them live.")
        self.change_btn.clicked.connect(self.configureBalanceRequested.emit)
        bar.addWidget(self.change_btn)
        self.info = QLabel("no calibration loaded — set the balance .vol "
                           "in the StrainBook device panel (Forces tab)")
        self.info.setObjectName("dim")
        # runtime text (cal summaries, error strings) can be long — never
        # let it define the panel's minimum width; it clips instead
        self.info.setMinimumWidth(1)
        bar.addWidget(self.info, 1)
        root.addLayout(bar)

        self.alarm = QLabel("")
        self.alarm.setStyleSheet(
            f"background-color: {theme.ERROR}; color: white; "
            "font-weight: bold; padding: 6px; border-radius: 4px;")
        self.alarm.setVisible(False)
        root.addWidget(self.alarm)

        tiles = QHBoxLayout()
        tiles.setSpacing(6)
        self.tiles: Dict[str, _Tile] = {}
        for i, (name, unit) in enumerate(_TILES):
            tile = _Tile(name, unit, theme.series_color(i))
            self.tiles[name] = tile
            tiles.addWidget(tile)
        root.addLayout(tiles)

        limits = QGroupBox("Element loads vs balance maxima   "
                           f"(peak marker: last {PEAK_HOLD_S:.0f} s, "
                           "resets on tare)")
        lg = QGridLayout(limits)
        lg.setVerticalSpacing(6)
        self.util_bars: Dict[int, LoadBar] = {}
        self.util_labels: Dict[int, QLabel] = {}
        for i in range(6):
            lbl = QLabel(f"ch{i}")
            lbl.setFixedWidth(74)
            lg.addWidget(lbl, i % 3, (i // 3) * 3)
            bar = LoadBar()
            lg.addWidget(bar, i % 3, (i // 3) * 3 + 1)
            pct = QLabel("—")
            pct.setFixedWidth(64)
            pct.setStyleSheet("font-family: Consolas, monospace;")
            lg.addWidget(pct, i % 3, (i // 3) * 3 + 2)
            self.util_labels[i] = lbl
            self.util_bars[i] = bar
            bar._pct = pct
        lg.setColumnStretch(1, 1)
        lg.setColumnStretch(4, 1)
        for row in range(3):
            lg.setRowStretch(row, 1)
        # the load-limit group takes the page's remaining vertical space
        # (the bars expand with it)
        root.addWidget(limits, 1)

    # ── calibration (pointers owned by the DEVICE config) ────────────────
    def load_vol(self, path: str) -> bool:
        try:
            cal = load_balance_cal(path, self._loaded_fit)
        except Exception as exc:                       # noqa: BLE001
            self.info.setText(f"failed to load: {exc}")
            self.cal = None
            # no calibration → no live reduction → a stale overstress
            # alarm must not keep blocking acquisition
            self._reset_alarm()
            return False
        self.cal = cal
        self.config.vol_path = path
        self.config.cal_type = self._loaded_fit
        self.vol_lbl.setText(Path(path).name)
        self.info.setText(balance_summary(cal) + f"   [{Path(path).name}]")
        for i, name in enumerate(cal.force_channels[:6]):
            self.util_labels[i].setText(name)
            limit = cal.max_loads.values.get(name)
            self.util_bars[i]._pct.setToolTip(
                f"max {limit}" if limit else "no max-load entry")
        return True

    def clear_vol(self) -> None:
        """Drop the loaded calibration — the device's .vol pointer was
        cleared (StrainBook panel → Forces tab → Clear).

        Stops the live reduction (back to the "no cal loaded" state) and —
        critically — RESETS the overstress alarm: ``record_blocker()`` is
        registered in ``manager.extra_blockers``, and ``overstress`` is
        otherwise only cleared inside :meth:`_update_util`, which can never
        run again without a cal, so a stale alarm would block acquisition
        forever."""
        self.cal = None
        self.config.vol_path = ""
        self._reset_alarm()
        self.vol_lbl.setText("—")
        self.info.setText("no calibration loaded — set the balance .vol "
                          "in the StrainBook device panel (Forces tab)")
        for name, _u in _TILES:
            self.tiles[name].set_value(None)
        self._reset_peaks()
        for i in range(6):
            self.util_labels[i].setText(f"ch{i}")
            bar = self.util_bars[i]
            bar.set_load(None, None, theme.SUCCESS)
            bar._pct.setText("—")
            bar._pct.setToolTip("")

    def _rolling_peak(self, name: str, u: float, now: float) -> float:
        h = self._peak_hist.setdefault(name, deque())
        h.append((now, u))
        cutoff = now - PEAK_HOLD_S
        while h and h[0][0] < cutoff:
            h.popleft()
        return max(v for _t, v in h)

    def _reset_peaks(self) -> None:
        self._peak_hist.clear()

    def _reset_alarm(self) -> None:
        """Clear the overstress state + banner (no cal → no blocker)."""
        self.overstress = False
        self.alarm.setVisible(False)

    def _sync_from_device(self) -> None:
        """Inherit .vol / fit / layout from the balance adapter (the
        device driver owns all three). Runs every tick, even while idle,
        so an edit in the embedded StrainBook panel — load, refit, clear,
        layout flip — reaches this readout within 200 ms. Devices without
        the attributes (external ATE balance, fakes) are left alone."""
        bal = self._balance
        layout = getattr(bal, "balance_config", None) \
            if bal is not None else None
        if layout:
            self._layout = layout
            self.config.balance_config = layout
            if self.layout_lbl.text() != layout:
                self.layout_lbl.setText(layout)
        vol = getattr(bal, "vol_path", None) if bal is not None else None
        fit = getattr(bal, "cal_type", None) if bal is not None else None
        if vol is None and fit is None:
            return                       # device doesn't own a .vol
        vol = vol or ""
        fit = fit or self._loaded_fit
        if vol == self._loaded_vol and fit == self._loaded_fit:
            return
        self._loaded_vol = vol
        self._loaded_fit = fit
        if self.fit_lbl.text() != fit:
            self.fit_lbl.setText(fit)
        if vol:
            self.load_vol(vol)
        else:
            self.clear_vol()

    # ── registry / discovery ─────────────────────────────────────────────
    def set_manager(self, manager) -> None:
        self.manager = manager
        self._discover()

    def _discover(self) -> None:
        bal = self.manager.by_role("balance")
        self._balance = bal if isinstance(bal, Streaming) else None
        self._daq = None
        for s in self.manager.streaming:
            try:
                if any(ch.group == "DaqBook2005" for ch in s.channels()):
                    self._daq = s
                    break
            except Exception:                          # noqa: BLE001
                continue

    # ── record interlock hook ────────────────────────────────────────────
    def record_blocker(self) -> Optional[str]:
        if self.overstress:
            return "BALANCE OVERSTRESS — reduce load before recording"
        return None

    # ── live sampling ────────────────────────────────────────────────────
    def _q_psi(self) -> Optional[float]:
        if self._daq is None:
            return None
        try:
            v = self._daq.latest()
            st = tunnel_state(float(v.get("Pdiff", 0.0)),
                              float(v.get("Ptot", 0.0)),
                              float(v.get("Temp", 0.0)))
            return st.q_psi if st.valid else None
        except Exception:                              # noqa: BLE001
            return None

    def _alpha_beta(self):
        pos = self.manager.positioner
        if pos is None:
            return 0.0, 0.0
        try:
            p = pos.positions()
            return float(p.get("alpha", 0.0)), float(p.get("beta", 0.0))
        except Exception:                              # noqa: BLE001
            return 0.0, 0.0

    def _sample(self) -> None:
        # inherit .vol/fit/layout from the device every tick, even while
        # idle (the propagation the operator expects)
        self._sync_from_device()
        # tare (any path: device panel button, engine zero) resets the
        # peak-hold markers
        zc = getattr(self._balance, "zero_count", None) \
            if self._balance is not None else None
        if zc is not None and zc != self._last_zero_count:
            self._last_zero_count = zc
            self._reset_peaks()
        if not self.active or self._balance is None:
            # not evaluating → a latched overstress must DECAY, not
            # persist as a stale record blocker
            self._reset_alarm()
            return
        raw_tail = getattr(self._balance, "raw_tail", None)
        if callable(raw_tail) and self.cal is not None:
            self._sample_calibrated(raw_tail)
        else:
            self._sample_resolved()      # owns overstress/alarm decay

    def _sample_calibrated(self, raw_tail) -> None:
        try:
            rate = self._balance.sample_rate()
        except Exception:                              # noqa: BLE001
            rate = 200.0
        n = max(int(WINDOW_S * max(rate, 10.0)), 2)
        raw = raw_tail(n)
        if len(raw) < 6:
            self.info.setText("calibration loaded, waiting for balance "
                              "channels…")
            return
        alpha, beta = self._alpha_beta()
        geom = Geometry(self.config.ref_area, self.config.ref_chord,
                        self.config.ref_span)
        try:
            res = compute_aero(raw, self.cal, alpha, beta,
                               self._layout,
                               q=self._q_psi(), geom=geom,
                               warn_utilization=self.config.warn_utilization)
        except Exception as exc:                       # noqa: BLE001
            self.info.setText(f"force computation failed: {exc}")
            return
        means = res.means()
        for name, _u in _TILES:
            self.tiles[name].set_value(means.get(name))
        self._update_util(res)

    def _sample_resolved(self) -> None:
        """External balance already streams resolved loads under their
        real names (Lift/Drag/Side/Pitch/Yaw/Roll) — show them, and run
        the element-load bars against the adapter's ``load_limits``."""
        try:
            vals = self._balance.latest()
        except Exception:                              # noqa: BLE001
            return
        for name, _u in _TILES:
            v = vals.get(name)
            self.tiles[name].set_value(None if v is None else float(v))
        if not any(n in vals for n in _RESOLVED_ORDER):
            # no resolved loads to evaluate (e.g. raw-volt balance without
            # a cal) → a latched overstress must DECAY, not persist as a
            # stale record blocker
            self._reset_alarm()
            return
        if self.cal is None:
            self.info.setText("external balance streams resolved loads "
                              "(no .vol needed)")
        self._update_resolved_bars(vals)

    def _update_resolved_bars(self, vals: Dict[str, float]) -> None:
        """Element-load bars for the resolved-load path: |load| vs the
        adapter's rated maxima. Channels without a rated max (0/missing)
        show the live load VALUE instead of a fake utilization. Overstress
        (any |load| >= max) raises the same banner/record blocker as the
        calibrated path, with identical decay."""
        limits = getattr(self._balance, "load_limits", None) or {}
        chan_units: Dict[str, str] = {}
        try:
            chan_units = {ch.name: ch.unit
                          for ch in self._balance.channels()}
        except Exception:                              # noqa: BLE001
            pass
        warn = self.config.warn_utilization
        now = time.monotonic()
        worst_name, worst_u = "", None
        for i, name in enumerate(_RESOLVED_ORDER):
            bar = self.util_bars[i]
            if self.util_labels[i].text() != name:
                self.util_labels[i].setText(name)
            v = vals.get(name)
            try:
                maxv = float(limits.get(name) or 0.0)
            except (TypeError, ValueError):
                maxv = 0.0
            if v is None:
                bar.set_load(None, None, theme.SUCCESS)
                bar._pct.setText("—")
                bar._pct.setToolTip("")
                continue
            if maxv > 0:
                u = abs(float(v)) / maxv
                peak = self._rolling_peak(name, u, now)
                color = (theme.SUCCESS if u < warn else
                         (theme.WARNING if u < 1.0 else theme.ERROR))
                bar.set_load(u, peak, color)
                bar._pct.setText(f"{u * 100:5.1f}%")
                bar._pct.setToolTip(
                    f"max {maxv:g} {chan_units.get(name, '')}".rstrip())
                if worst_u is None or u > worst_u:
                    worst_name, worst_u = name, u
            else:
                # no rated max known → honest value, neutral/empty bar
                bar.set_load(None, None, theme.SUCCESS)
                unit = chan_units.get(name, "")
                bar._pct.setText(f"{float(v):+.1f} {unit}".rstrip())
                bar._pct.setToolTip("no rated max-load entry — live load "
                                    "value shown")
        self.overstress = worst_u is not None and worst_u >= 1.0
        if self.overstress:
            self.alarm.setText(
                f"⚠ BALANCE OVERSTRESS: {worst_name} at "
                f"{worst_u * 100:.0f}% of rated load — recording blocked")
            self.alarm.setStyleSheet(
                f"background-color: {theme.ERROR}; color: white; "
                "font-weight: bold; padding: 6px; border-radius: 4px;")
            self.alarm.setVisible(True)
        elif worst_u is not None and worst_u >= warn:
            self.alarm.setText(
                f"⚠ approaching limit: {worst_name} at "
                f"{worst_u * 100:.0f}% of rated load")
            self.alarm.setStyleSheet(
                f"background-color: {theme.WARNING}; color: black; "
                "font-weight: bold; padding: 6px; border-radius: 4px;")
            self.alarm.setVisible(True)
        else:
            self.alarm.setVisible(False)

    def _update_util(self, res) -> None:
        self.overstress = res.overstress
        warn = self.config.warn_utilization
        now = time.monotonic()
        for i, name in enumerate(self.cal.force_channels[:6]):
            u = res.utilization.get(name)
            bar = self.util_bars[i]
            if u is None:
                bar.set_load(None, None, theme.SUCCESS)
                bar._pct.setText("n/a")
                continue
            peak = self._rolling_peak(name, u, now)
            color = (theme.SUCCESS if u < warn else
                     (theme.WARNING if u < 1.0 else theme.ERROR))
            bar.set_load(u, peak, color)
            bar._pct.setText(f"{u * 100:5.1f}%")
        if res.overstress:
            self.alarm.setText(
                f"⚠ BALANCE OVERSTRESS: {res.worst_channel} at "
                f"{res.worst_util * 100:.0f}% of rated load — recording "
                "blocked")
            self.alarm.setStyleSheet(
                f"background-color: {theme.ERROR}; color: white; "
                "font-weight: bold; padding: 6px; border-radius: 4px;")
            self.alarm.setVisible(True)
        elif res.warn:
            self.alarm.setText(
                f"⚠ approaching limit: {res.worst_channel} at "
                f"{res.worst_util * 100:.0f}% of rated load")
            self.alarm.setStyleSheet(
                f"background-color: {theme.WARNING}; color: black; "
                "font-weight: bold; padding: 6px; border-radius: 4px;")
            self.alarm.setVisible(True)
        else:
            self.alarm.setVisible(False)

    def shutdown(self) -> None:
        self._timer.stop()
