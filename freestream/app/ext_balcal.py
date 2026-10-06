"""External Balance Calibration — SPLAT dead-weight check for the ATE.

Advanced ▸ External Balance Calibration. Much simpler than the internal
balcal_gui: a SPLAT loading applies the SAME dead-weight increment to
ALL six channels at once via the loading fixture; the operator steps
the load up and back down, capturing a window of unsteady data at each
step. The window shows, per channel (3×2 grid):

* step means with sigma error bars vs applied load, plus a live
  linear fit and R2,
* the fit RESIDUALS vs load, or
* the ERROR MODEL view, which is the point of the exercise: |residual|
  and the within-step sigma are plotted against |load| and fitted
  against two competing laws, ``E = c`` (constant, %-of-full-scale)
  and ``E = k|L|`` (proportional, %-of-reading). The models are
  ranked by RMS misfit and a free power law ``E = c|L|^p`` is fitted
  as a second opinion: p near 1 means the error follows the reading,
  p near 0 means it follows full scale.

Both error measures are reported because they answer different
questions. The residual of the step means carries systematic error
(nonlinearity, hysteresis) but has one value per step. The within-step
sigma is backed by every frame of the capture, so it pins the RANDOM
error at each load far more tightly.

ALL unsteady data (every frame of every step) is saved to one .mat
organized like the suite's other IO files: device group ``ATE_Balance``
with per-channel sample arrays, a ``Time`` group, plus ``Steps``,
``Fits`` and ``meta`` structs.

Loads are entered in lb or kg and stored canonically in lb. The ATE
streams in whatever unit the OGI is configured for and never reports
it on the wire; cross-checking a 2026-08-20 run against the SPLAT
archive gives a sensitivity ratio of 0.998 against files that declare
lb, so the stream is pounds and lb-ft. Uses the full ATE device driver via the freestream
adapter (shared live adapter when Freestream is connected in an ATE
mode, standalone otherwise — sim supported end to end).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from .. import theme

log = logging.getLogger(__name__)

LB_PER_KG = 2.2046226218
# Balance-frame channels as the ATE adapter streams them (X back, Y
# right, Z up): Fz carries an applied vertical load, Fx a drag-direction
# load, and so on.
CHANNELS = ("Fx", "Fy", "Fz", "Mx", "My", "Mz")
_MOMENT_CHANNELS = ("Mx", "My", "Mz")


def linfit(x: np.ndarray, y: np.ndarray):
    """Least-squares line: (slope, intercept, r_squared)."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if x.size < 2 or np.ptp(x) == 0:
        return 0.0, float(np.mean(y)) if y.size else 0.0, 0.0
    m, b = np.polyfit(x, y, 1)
    pred = m * x + b
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 1.0
    return float(m), float(b), float(r2)


def model_compare(loads: np.ndarray, err: np.ndarray,
                  full_scale: float = 0.0) -> Dict:
    """Decide whether an error magnitude scales with the READING or is
    constant (%-of-full-scale).

    Fits two competing models to ``err`` = |error| at applied |load|::

        full scale:   E = c          (constant, load independent)
        reading:      E = k * |L|    (proportional, through the origin)

    and compares them by RMS misfit, so the winner is a direct model
    selection rather than a correlation eyeball. Also fits the free
    power law ``E = c * |L| ** p``: p near 1 is proportional to
    reading, p near 0 is constant. Folding a signed residual through
    ``abs()`` biases the SCALE (for Gaussian noise E|e| = 0.798 sigma)
    but leaves the EXPONENT unbiased, which is why p is the robust
    discriminator.
    """
    x = np.abs(np.asarray(loads, dtype=float))
    e = np.abs(np.asarray(err, dtype=float))
    ok = np.isfinite(x) & np.isfinite(e)
    x, e = x[ok], e[ok]
    out = {"c": 0.0, "k": 0.0, "rms_fs": 0.0, "rms_rdg": 0.0,
           "p": float("nan"), "p_r2": 0.0, "corr": 0.0,
           "prefers": "insufficient", "margin": 0.0}
    if e.size < 3:
        return out
    c = float(np.mean(e))
    out["c"] = c
    out["rms_fs"] = float(np.sqrt(np.mean((e - c) ** 2)))
    denom = float(np.sum(x * x))
    k = float(np.sum(x * e) / denom) if denom > 0 else 0.0
    out["k"] = k
    out["rms_rdg"] = float(np.sqrt(np.mean((e - k * x) ** 2)))
    if np.ptp(x) > 0 and np.ptp(e) > 0:
        out["corr"] = float(np.corrcoef(x, e)[0, 1])
    m = (x > 0) & (e > 0)
    if int(m.sum()) >= 3 and np.ptp(x[m]) > 0:
        p, _logc, p_r2 = linfit(np.log(x[m]), np.log(e[m]))
        out["p"], out["p_r2"] = p, p_r2
    # model selection: whichever model leaves less unexplained error,
    # requiring a 20 % margin before calling it either way
    lo, hi = sorted((out["rms_fs"], out["rms_rdg"]))
    out["margin"] = (1.0 - lo / hi) * 100.0 if hi > 0 else 0.0
    if out["rms_rdg"] < 0.8 * out["rms_fs"]:
        out["prefers"] = "reading"
    elif out["rms_fs"] < 0.8 * out["rms_rdg"]:
        out["prefers"] = "full scale"
    else:
        out["prefers"] = "mixed"
    return out


def residual_analysis(loads: np.ndarray, means: np.ndarray,
                      stds: Optional[np.ndarray] = None,
                      full_scale: float = 0.0) -> Dict:
    """Fit the calibration line, then ask what the leftover error
    scales with.

    Two independent error measures are tested against the same pair of
    models (see :func:`model_compare`):

    ``rdg``
        |residual| of the step MEANS. Carries systematic error
        (nonlinearity, hysteresis) plus step-to-step scatter. One
        value per step, so it is the noisier of the two.
    ``sig``
        The within-step standard deviation from the unsteady capture.
        Hundreds of samples back every point, so it measures the
        RANDOM error at that load far more precisely. Only available
        when ``stds`` is supplied.

    ``pct_of_reading`` converts the proportional coefficient into a
    unit-free percentage: the channel reads in N per lb of applied
    load, so ``k / slope`` is dimensionless.
    """
    loads = np.asarray(loads, dtype=float)
    means = np.asarray(means, dtype=float)
    slope, icpt, r2 = linfit(loads, means)
    resid = means - (slope * loads + icpt)
    a_l, a_r = np.abs(loads), np.abs(resid)
    r_slope, r_icpt, _ = linfit(a_l, a_r)
    corr = (float(np.corrcoef(a_l, a_r)[0, 1])
            if a_l.size > 2 and np.ptp(a_l) > 0 and np.ptp(a_r) > 0
            else 0.0)
    pct = abs(r_slope / slope) * 100.0 if slope else 0.0
    rdg = model_compare(loads, resid, full_scale)
    sig = (model_compare(loads, stds, full_scale)
           if stds is not None and np.size(stds) else None)
    resid_rms = float(np.sqrt(np.mean(resid ** 2))) if resid.size else 0.0
    out = {"slope": slope, "intercept": icpt, "r_squared": r2,
           "resid": resid, "resid_rms": resid_rms,
           "resid_slope": r_slope, "resid_intercept": r_icpt,
           "resid_corr": corr, "pct_of_reading": pct,
           "rdg": rdg, "sig": sig, "full_scale": float(full_scale)}
    # percentages that stand on their own: proportional coefficient as
    # a % of the reading, constant coefficient as a % of rated load
    out["pct_reading_fit"] = (abs(rdg["k"] / slope) * 100.0
                              if slope else 0.0)
    out["pct_fs_fit"] = (rdg["c"] / full_scale * 100.0
                         if full_scale else 0.0)
    if sig is not None:
        out["sig_pct_reading"] = (abs(sig["k"] / slope) * 100.0
                                  if slope else 0.0)
        out["sig_pct_fs"] = (sig["c"] / full_scale * 100.0
                             if full_scale else 0.0)
    return out


class ExternalBalCalWindow(QMainWindow):
    """SPLAT dead-weight calibration check for the ATE external balance."""

    def __init__(self, adapter=None, sim: bool = True,
                 data_root: str = "runs", parent=None):
        super().__init__(parent)
        self.setWindowTitle("External Balance Calibration — SPLAT check")
        self.resize(1240, 860)
        self.setStyleSheet(theme.get_stylesheet())
        self._shared = adapter is not None
        if adapter is None:
            from ..adapters.ate import AteBalanceAdapter
            adapter = AteBalanceAdapter(sim=sim)
        self.adapter = adapter
        self.data_root = Path(data_root)

        self.applied_lb = 0.0
        self.steps: List[Dict] = []
        self._cap_buf: Optional[Dict[str, list]] = None
        self._cap_end = 0.0
        self._cap_dir = ""

        self._build_ui()
        self._cap_timer = QTimer(self)
        self._cap_timer.setInterval(100)
        self._cap_timer.timeout.connect(self._cap_tick)
        if self._shared:
            self.conn_btn.hide()
            self.disc_btn.hide()
            self._set_status("sharing the live ATE adapter")
        self._update_enables()

    # ── UI ───────────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QWidget()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setSpacing(8)

        bar = QHBoxLayout()
        box = QGroupBox("SPLAT loading")
        b = QHBoxLayout(box)
        b.addWidget(QLabel("Load step"))
        self.load_spin = QDoubleSpinBox()
        self.load_spin.setRange(0.0001, 10000.0)
        self.load_spin.setDecimals(4)
        self.load_spin.setValue(1.0)
        b.addWidget(self.load_spin)
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(["lb", "kg"])
        b.addWidget(self.unit_combo)
        b.addSpacing(12)
        b.addWidget(QLabel("Rate"))
        self.rate_spin = QDoubleSpinBox()
        self.rate_spin.setRange(1.0, 1000.0)
        self.rate_spin.setDecimals(0)
        self.rate_spin.setValue(float(self.adapter.sample_rate() or 50.0))
        self.rate_spin.setSuffix(" Hz")
        self.rate_spin.setToolTip(
            "The OGI pushes frames at its own fixed rate — this sets the "
            "recorded time base and the capture sizing only.")
        b.addWidget(self.rate_spin)
        b.addWidget(QLabel("Duration"))
        self.dur_spin = QDoubleSpinBox()
        self.dur_spin.setRange(0.2, 120.0)
        self.dur_spin.setDecimals(1)
        self.dur_spin.setValue(5.0)
        self.dur_spin.setSuffix(" s")
        b.addWidget(self.dur_spin)
        bar.addWidget(box, 1)

        act = QGroupBox("Capture")
        a = QHBoxLayout(act)
        self.zero_btn = QPushButton("Tare (OGI zero)")
        self.zero_btn.clicked.connect(self._tare)
        a.addWidget(self.zero_btn)
        self.base_btn = QPushButton("Capture 0-load point")
        self.base_btn.clicked.connect(lambda: self._capture("zero"))
        a.addWidget(self.base_btn)
        self.up_btn = QPushButton("▲ +step, capture")
        self.up_btn.setObjectName("primary")
        self.up_btn.setToolTip("Add ONE load increment to the fixture, "
                               "then capture")
        self.up_btn.clicked.connect(lambda: self._capture("up"))
        a.addWidget(self.up_btn)
        self.dn_btn = QPushButton("▼ −step, capture")
        self.dn_btn.setToolTip("Remove ONE load increment, then capture")
        self.dn_btn.clicked.connect(lambda: self._capture("down"))
        a.addWidget(self.dn_btn)
        self.undo_btn = QPushButton("Undo last")
        self.undo_btn.clicked.connect(self._undo)
        a.addWidget(self.undo_btn)
        self.clear_btn = QPushButton("Clear")
        self.clear_btn.clicked.connect(self._clear)
        a.addWidget(self.clear_btn)
        self.save_btn = QPushButton("Save .mat…")
        self.save_btn.setObjectName("success")
        self.save_btn.clicked.connect(self._save)
        a.addWidget(self.save_btn)
        bar.addWidget(act, 1)

        conn = QGroupBox("Device")
        c = QHBoxLayout(conn)
        self.conn_btn = QPushButton("Connect")
        self.conn_btn.clicked.connect(self._connect)
        c.addWidget(self.conn_btn)
        self.disc_btn = QPushButton("Disconnect")
        self.disc_btn.clicked.connect(self._disconnect)
        c.addWidget(self.disc_btn)
        self.view_combo = QComboBox()
        self.view_combo.addItems(["Means + fit", "Residuals",
                                  "Error model"])
        self.view_combo.setToolTip(
            "Error model: |residual| and within-step sigma against "
            "|load|, with both candidate laws overlaid — a flat line "
            "is %-of-full-scale, a ray through the origin is "
            "%-of-reading.")
        self.view_combo.currentIndexChanged.connect(self._redraw)
        c.addWidget(self.view_combo)
        bar.addWidget(conn)
        lay.addLayout(bar)

        self.status_lbl = QLabel("idle — applied load 0.0000 lb")
        self.status_lbl.setProperty("mono", "true")
        lay.addWidget(self.status_lbl)

        # ── 3×2 channel grid ──
        grid_w = QWidget()
        grid = QGridLayout(grid_w)
        grid.setSpacing(6)
        self.plots: Dict[str, pg.PlotWidget] = {}
        self.curve_pts: Dict[str, pg.ErrorBarItem] = {}
        self.curve_sc: Dict[str, pg.PlotDataItem] = {}
        self.curve_fit: Dict[str, pg.PlotDataItem] = {}
        self.curve_sig: Dict[str, pg.PlotDataItem] = {}
        self.curve_alt: Dict[str, pg.PlotDataItem] = {}
        for i, ch in enumerate(CHANNELS):
            pw = pg.PlotWidget()
            pi = pw.getPlotItem()
            pi.showGrid(x=True, y=True, alpha=0.2)
            pi.setTitle(ch)
            pi.setLabel("bottom", "applied load  (lb)")
            unit = "N·m" if ch in _MOMENT_CHANNELS else "N"
            pi.setLabel("left", f"{ch}  ({unit})")
            color = theme.series_color(i)
            self.curve_pts[ch] = pg.ErrorBarItem(
                pen=pg.mkPen(color, width=1))
            pi.addItem(self.curve_pts[ch])
            self.curve_sc[ch] = pi.plot(
                [], [], pen=None, symbol="o", symbolSize=6,
                symbolBrush=color)
            self.curve_fit[ch] = pi.plot(
                [], [], pen=pg.mkPen(theme.TEXT_DIM, width=1,
                                     style=pg.QtCore.Qt.PenStyle.DashLine))
            # error-model view: sigma points + the second candidate law
            self.curve_sig[ch] = pi.plot(
                [], [], pen=None, symbol="t", symbolSize=6,
                symbolBrush=theme.TEXT_DIM)
            self.curve_alt[ch] = pi.plot(
                [], [], pen=pg.mkPen(theme.WARNING, width=1,
                                     style=pg.QtCore.Qt.PenStyle.DotLine))
            self.plots[ch] = pw
            grid.addWidget(pw, i // 3, i % 3)
        lay.addWidget(grid_w, 1)

        # ── per-channel stats table ──
        self.table = QTableWidget(len(CHANNELS), 10)
        self.table.setHorizontalHeaderLabels(
            ["Channel", "mean (last)", "σ (last)", "slope /lb",
             "R²", "resid RMS", "exponent p", "% of reading",
             "% of FS", "verdict"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        mono = QFont("Consolas")
        self.table.setFont(mono)
        self.table.setMaximumHeight(230)
        for r, ch in enumerate(CHANNELS):
            self.table.setItem(r, 0, QTableWidgetItem(ch))
            for c in range(1, 10):
                self.table.setItem(r, c, QTableWidgetItem("--"))
        lay.addWidget(self.table)
        theme.install_wheel_guard(self)

    # ── device ───────────────────────────────────────────────────────────
    def _connect(self):
        try:
            self.adapter.connect()
            self.adapter.start()
            self._set_status("connected"
                             + (" (SIM)" if self.adapter.sim else ""))
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.warning(self, "External Balance", str(exc))
        self._update_enables()

    def _disconnect(self):
        try:
            self.adapter.stop()
            self.adapter.disconnect()
        except Exception as exc:                       # noqa: BLE001
            log.warning("disconnect: %s", exc)
        self._set_status("disconnected")
        self._update_enables()

    def _tare(self):
        try:
            self.adapter.zero()
            self._set_status("OGI tare sent")
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.warning(self, "External Balance", str(exc))

    # ── capture ──────────────────────────────────────────────────────────
    def _step_lb(self) -> float:
        v = float(self.load_spin.value())
        return v * LB_PER_KG if self.unit_combo.currentText() == "kg" \
            else v

    def _capture(self, direction: str):
        if not self.adapter.connected:
            QMessageBox.warning(self, "External Balance",
                                "Connect the ATE first")
            return
        if self._cap_buf is not None:
            return
        if direction == "up":
            self.applied_lb += self._step_lb()
        elif direction == "down":
            self.applied_lb -= self._step_lb()
        self.adapter.drain_block()          # flush pre-capture frames
        self._cap_buf = {ch: [] for ch in CHANNELS}
        self._cap_dir = direction
        self._cap_t0 = time.time()
        self._cap_end = self._cap_t0 + float(self.dur_spin.value())
        self._set_status(
            f"capturing {self.dur_spin.value():.1f} s at "
            f"{self.applied_lb:+.4f} lb …")
        self._update_enables()
        self._cap_timer.start()

    def _cap_tick(self):
        block = self.adapter.drain_block()
        for ch in CHANNELS:
            arr = block.get(ch)
            if arr is not None and np.size(arr):
                self._cap_buf[ch].extend(np.atleast_1d(arr).tolist())
        if time.time() < self._cap_end:
            return
        self._cap_timer.stop()
        data = {ch: np.asarray(self._cap_buf[ch], dtype=float)
                for ch in CHANNELS}
        self._cap_buf = None
        n = min((v.size for v in data.values()), default=0)
        if n < 2:
            # roll back the applied-load bookkeeping for a dud capture
            if self._cap_dir == "up":
                self.applied_lb -= self._step_lb()
            elif self._cap_dir == "down":
                self.applied_lb += self._step_lb()
            self._set_status("capture produced no frames — check the "
                             "ATE stream")
            self._update_enables()
            return
        data = {ch: v[:n] for ch, v in data.items()}
        self.steps.append({
            "load_lb": self.applied_lb,
            "load_display": float(self.load_spin.value()),
            "unit": self.unit_combo.currentText(),
            "direction": self._cap_dir,
            "t_start": self._cap_t0, "n": n, "data": data,
            "mean": {ch: float(np.mean(v)) for ch, v in data.items()},
            "std": {ch: float(np.std(v)) for ch, v in data.items()},
        })
        self._set_status(
            f"step {len(self.steps)}: {self.applied_lb:+.4f} lb, "
            f"{n} frames")
        self._redraw()
        self._update_enables()

    def _undo(self):
        if not self.steps:
            return
        s = self.steps.pop()
        if s["direction"] == "up":
            self.applied_lb -= self._step_lb()
        elif s["direction"] == "down":
            self.applied_lb += self._step_lb()
        self._set_status(f"undid step — applied load "
                         f"{self.applied_lb:+.4f} lb")
        self._redraw()
        self._update_enables()

    def _clear(self):
        self.steps = []
        self.applied_lb = 0.0
        self._set_status("cleared — applied load 0.0000 lb")
        self._redraw()
        self._update_enables()

    def _limits(self) -> Dict[str, float]:
        """load_limits is a property on the ATE adapter but a method on
        some other adapters — accept either."""
        v = getattr(self.adapter, "load_limits", None)
        if callable(v):
            try:
                v = v()
            except Exception:                          # noqa: BLE001
                v = {}
        return dict(v or {})

    # ── analysis + plots ─────────────────────────────────────────────────
    def analyses(self) -> Dict[str, Dict]:
        """Per-channel fit + error-model comparison. The within-step
        sigma and the channel's rated load are handed in so the random
        error gets its own model test and the constant term can be
        quoted as a true %FS."""
        loads = np.array([s["load_lb"] for s in self.steps])
        limits = self._limits()
        out = {}
        for ch in CHANNELS:
            if loads.size < 2:
                out[ch] = None
                continue
            means = np.array([s["mean"][ch] for s in self.steps])
            stds = np.array([s["std"][ch] for s in self.steps])
            out[ch] = residual_analysis(
                loads, means, stds=stds,
                full_scale=float(limits.get(ch, 0.0) or 0.0))
        return out

    def _redraw(self):
        loads = np.array([s["load_lb"] for s in self.steps])
        mode = self.view_combo.currentIndex()
        fits = self.analyses()
        for r, ch in enumerate(CHANNELS):
            means = np.array([s["mean"][ch] for s in self.steps])
            stds = np.array([s["std"][ch] for s in self.steps])
            fa = fits[ch]
            pi = self.plots[ch].getPlotItem()
            unit = "N·m" if ch in _MOMENT_CHANNELS else "N"
            self.curve_sig[ch].setData([], [])
            self.curve_alt[ch].setData([], [])
            if mode == 2 and fa is not None:
                # error magnitude vs |load|, both candidate laws drawn
                a_l = np.abs(loads)
                a_r = np.abs(fa["resid"])
                self.curve_pts[ch].setData(x=np.array([]),
                                           y=np.array([]),
                                           height=np.array([]))
                self.curve_sc[ch].setData(a_l, a_r)
                self.curve_sig[ch].setData(a_l, stds)
                xs = np.linspace(0.0, a_l.max(), 2) if a_l.size                     else np.array([])
                rdg = fa["rdg"]
                self.curve_fit[ch].setData(xs, rdg["k"] * xs)
                self.curve_alt[ch].setData(xs, np.full(xs.shape,
                                                       rdg["c"]))
                pi.setLabel("bottom", "|applied load|  (lb)")
                pi.setLabel("left", f"|error|  ({unit})")
            elif mode == 1 and fa is not None:
                y = fa["resid"]
                self.curve_pts[ch].setData(x=loads, y=y, height=2 * stds)
                self.curve_sc[ch].setData(loads, y)
                xs = np.linspace(min(0, loads.min()), loads.max(), 2)                     if loads.size else np.array([])
                self.curve_fit[ch].setData(
                    xs, fa["resid_intercept"] + fa["resid_slope"]
                    * np.abs(xs))
                pi.setLabel("bottom", "applied load  (lb)")
                pi.setLabel("left", f"{ch} residual  ({unit})")
            else:
                self.curve_pts[ch].setData(x=loads, y=means,
                                           height=2 * stds)
                self.curve_sc[ch].setData(loads, means)
                if fa is not None and loads.size:
                    xs = np.array([loads.min(), loads.max()])
                    self.curve_fit[ch].setData(
                        xs, fa["slope"] * xs + fa["intercept"])
                else:
                    self.curve_fit[ch].setData([], [])
                pi.setLabel("bottom", "applied load  (lb)")
                pi.setLabel("left", f"{ch}  ({unit})")
            title = ch
            if fa is not None:
                title += f"   R²={fa['r_squared']:.5f}"
                if mode == 2:
                    title += f"   p={fa['rdg']['p']:.2f}"
            pi.setTitle(title)
            self._fill_row(r, ch, fa, means, stds)

    def _fill_row(self, r: int, ch: str, fa, means, stds) -> None:
        def _set(col, text):
            self.table.item(r, col).setText(text)

        if self.steps:
            _set(1, f"{means[-1]:+.4f}")
            _set(2, f"{stds[-1]:.4f}")
        if fa is None:
            return
        rdg, sig = fa["rdg"], fa["sig"]
        _set(3, f"{fa['slope']:+.5f}")
        _set(4, f"{fa['r_squared']:.5f}")
        _set(5, f"{fa['resid_rms']:.5f}")
        _set(6, f"{rdg['p']:+.2f} (r²={rdg['p_r2']:.2f})")
        _set(7, f"{fa['pct_reading_fit']:.3f} %")
        _set(8, (f"{fa['pct_fs_fit']:.3f} %" if fa["full_scale"]
                 else "no rated load"))
        # verdict: the model comparison on the step means, plus the
        # independent (and much better sampled) sigma test
        verdict = {"reading": "∝ READING", "full scale": "∝ full scale",
                   "mixed": "mixed", "insufficient": "need ≥3 steps"}
        v = f"{verdict[rdg['prefers']]} ({rdg['margin']:.0f} %)"
        if sig is not None and sig["prefers"] != "insufficient":
            v += f"   σ: {verdict[sig['prefers']]}"
        _set(9, v)

    # ── save ─────────────────────────────────────────────────────────────
    def _save(self):
        if not self.steps:
            QMessageBox.information(self, "External Balance",
                                    "No steps captured yet")
            return
        out_dir = self.data_root / "ext_balcal"
        out_dir.mkdir(parents=True, exist_ok=True)
        default = out_dir / datetime.now().strftime(
            "ext_balcal_%Y_%m_%d_%H%M%S.mat")
        path, _ = QFileDialog.getSaveFileName(
            self, "Save external balance calibration", str(default),
            "MAT (*.mat)")
        if not path:
            return
        try:
            self.save_mat(path)
            self._set_status(f"saved {Path(path).name}")
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.warning(self, "External Balance", str(exc))

    def save_mat(self, path) -> None:
        """ALL unsteady data, organized like the suite's other IO mats:
        device group with per-channel sample arrays + Time group +
        Steps/Fits/meta structs."""
        import scipy.io
        rate = float(self.rate_spin.value()) or 50.0
        starts, t_all, applied = [], [], []
        cat = {ch: [] for ch in CHANNELS}
        idx = 0
        t0 = self.steps[0]["t_start"]
        for s in self.steps:
            starts.append(idx)
            n = s["n"]
            for ch in CHANNELS:
                cat[ch].append(s["data"][ch])
            t_all.append((s["t_start"] - t0) + np.arange(n) / rate)
            applied.append(np.full(n, s["load_lb"]))
            idx += n
        fits = self.analyses()
        mat = {
            "ATE_Balance": {
                **{ch: np.concatenate(cat[ch]) for ch in CHANNELS},
                "AppliedLoad": np.concatenate(applied),
            },
            "Time": {"Time": np.concatenate(t_all)},
            "Steps": {
                "load_lb": np.array([s["load_lb"] for s in self.steps]),
                "direction": [s["direction"] for s in self.steps],
                "unit": [s["unit"] for s in self.steps],
                "n_samples": np.array([s["n"] for s in self.steps]),
                "start_index": np.array(starts),
                "t_start": np.array([s["t_start"] for s in self.steps]),
                "mean": {ch: np.array([s["mean"][ch]
                                       for s in self.steps])
                         for ch in CHANNELS},
                "std": {ch: np.array([s["std"][ch]
                                      for s in self.steps])
                        for ch in CHANNELS},
            },
            "meta": {
                "run": {"kind": "external_balance_cal",
                        "created": datetime.now().isoformat(
                            timespec="seconds"),
                        "load_unit": self.unit_combo.currentText(),
                        "step_load": float(self.load_spin.value()),
                        "sample_rate_hz": rate,
                        "duration_s": float(self.dur_spin.value()),
                        "sim": bool(self.adapter.sim)},
                "devices": {"ate": {
                    "balance_type": "external",
                    # The OGI streams in whatever unit it is
                    # configured for and does not report it on the
                    # wire. Cross-checking the 2026-08-20 run against
                    # the SPLAT archive (identical dead weights, same
                    # fixture) gives a sensitivity ratio of 0.998
                    # against files whose header declares Lb/.ft, so
                    # the stream is POUNDS. An earlier hardcoded
                    # "N / N.m" here was wrong by a factor of 4.448.
                    "load_units": "lb / lb-ft",
                    **{k: str(v) for k, v in
                       (self.adapter.extra_meta() or {}).items()},
                    "load_limits": {k: float(v) for k, v in
                                    self._limits().items()},
                }},
            },
        }
        if any(f is not None for f in fits.values()):
            def _clean(fa):
                out = {k: v for k, v in fa.items() if v is not None}
                out["resid"] = np.asarray(fa["resid"])
                return out
            mat["Fits"] = {ch: _clean(fa) for ch, fa in fits.items()
                           if fa is not None}
        scipy.io.savemat(path, mat, long_field_names=True)

    # ── plumbing ─────────────────────────────────────────────────────────
    def _set_status(self, msg: str):
        self.status_lbl.setText(
            f"{msg}   |   applied load {self.applied_lb:+.4f} lb, "
            f"{len(self.steps)} step(s)")

    def _update_enables(self):
        connected = bool(getattr(self.adapter, "connected", False))
        capturing = self._cap_buf is not None
        self.conn_btn.setEnabled(not connected)
        self.disc_btn.setEnabled(connected and not self._shared)
        for w in (self.zero_btn, self.base_btn, self.up_btn,
                  self.dn_btn):
            w.setEnabled(connected and not capturing)
        self.undo_btn.setEnabled(bool(self.steps) and not capturing)
        self.clear_btn.setEnabled(bool(self.steps) and not capturing)
        self.save_btn.setEnabled(bool(self.steps) and not capturing)

    def closeEvent(self, event):
        if not self._shared:
            try:
                self.adapter.stop()
                self.adapter.disconnect()
            except Exception:                          # noqa: BLE001
                pass
        super().closeEvent(event)
