"""Results panel — Lift-vs-α (and Cl/Cd/…) as the α/β matrix fleshes out.

Alongside the live streaming pane, this accumulates a REDUCED point every
time the sweep writes an ``.h5`` (:meth:`add_point`), so an operator watches
the polar build up in real time. Reduction is display-only and mirrors
Streamlined: the recorded raw StrainBook volts + the point's α/β + the
recorded ``/Tunnel/q_meas`` are pushed through :mod:`freestream.aero` into
wind-axis Lift/Drag and coefficients.

Two views:
* a scatter of the chosen metric vs α, with the up-sweep and down-sweep
  legs drawn as separate connected lines so **hysteresis loops are visible**
  (positive vs negative α̇), and
* an α–β map of visited points (which corners of the matrix are done).

For the external balance (Mode 2), the recorded ``ATE_Balance`` group holds
resolved wind-axis loads directly (true Lift/Drag/… names), so those are
used without a ``.vol``; the file's ``balance_group``/``balance_type``
markers identify the group. Legacy mode-2 files that aliased the loads
onto StrainBook names still load through the alias fallback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QVBoxLayout,
                             QWidget)

from .. import theme
from ..aero import Geometry, load_balance_cal, wind_axis
from ..recorder import read_point

# metric key → (label, needs coefficient?)
_METRICS = ["Lift", "Drag", "CL", "CD", "Pitch", "L/D"]
# LEGACY mode-2 files only: the retired file-parity aliasing that recorded
# resolved wind loads under StrainBook names (new files use the true
# Lift/Drag/… names in group ATE_Balance)
_LEGACY_ATE_ALIAS = {"Lift": "N1", "Pitch": "N2", "Side": "Y1",
                     "Yaw": "Y2", "Drag": "Axial", "Roll": "Roll"}


class ResultsPanel(QWidget):
    """Accumulating polar of reduced points from written .h5 files."""

    def __init__(self, manager, config, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.config = config
        self.active = False
        self._cal = None
        self._cal_path = ""
        self._rows: List[Dict[str, float]] = []
        self._build()

    def _build(self) -> None:
        import pyqtgraph as pg
        self._pg = pg
        root = QVBoxLayout(self)
        root.setSpacing(6)

        bar = QHBoxLayout()
        bar.addWidget(QLabel("Y"))
        self.metric = QComboBox()
        self.metric.addItems(_METRICS)
        self.metric.currentTextChanged.connect(lambda _: self._redraw())
        bar.addWidget(self.metric)
        self.count_lbl = QLabel("0 points")
        self.count_lbl.setObjectName("dim")
        bar.addWidget(self.count_lbl)
        bar.addStretch(1)
        clear = QLabel("")
        bar.addWidget(clear)
        root.addLayout(bar)

        glw = pg.GraphicsLayoutWidget()
        self._p_metric = glw.addPlot(row=0, col=0)
        self._p_metric.showGrid(x=True, y=True, alpha=0.25)
        self._p_metric.setLabel("bottom", "alpha [deg]")
        self._p_metric.setLabel("left", "Lift")
        self._p_metric.addLegend(offset=(10, 10),
                                 labelTextColor=theme.TEXT_DIM)
        self._p_map = glw.addPlot(row=0, col=1)
        self._p_map.showGrid(x=True, y=True, alpha=0.25)
        self._p_map.setLabel("bottom", "alpha [deg]")
        self._p_map.setLabel("left", "beta [deg]")
        self._p_map.setTitle("visited α–β matrix")
        root.addWidget(glw, 1)

        self._scatter_up = self._p_metric.plot(
            [], [], pen=pg.mkPen(theme.series_color(0), width=2),
            symbol="o", symbolSize=7,
            symbolBrush=theme.series_color(0), name="α̇ > 0 (up)")
        self._scatter_dn = self._p_metric.plot(
            [], [], pen=pg.mkPen(theme.series_color(5), width=2,
                                 style=Qt.PenStyle.DashLine),
            symbol="t", symbolSize=7,
            symbolBrush=theme.series_color(5), name="α̇ < 0 (down)")
        self._map_scatter = self._p_map.plot(
            [], [], pen=None, symbol="o", symbolSize=8,
            symbolBrush=theme.series_color(2))

    # ── registry / config ────────────────────────────────────────────────
    def set_manager(self, manager) -> None:
        self.manager = manager

    def clear(self) -> None:
        self._rows.clear()
        self._redraw()

    def _q_live_psi(self) -> Optional[float]:
        from ..derived import tunnel_state
        for s in self.manager.streaming:
            try:
                if any(ch.group == "DaqBook2005" for ch in s.channels()):
                    v = s.latest()
                    st = tunnel_state(float(v.get("Pdiff", 0.0)),
                                      float(v.get("Ptot", 0.0)),
                                      float(v.get("Temp", 0.0)))
                    return st.q_psi if st.valid else None
            except Exception:                          # noqa: BLE001
                continue
        return None

    def _ensure_cal(self):
        if self.config.vol_path and self._cal_path != self.config.vol_path:
            try:
                self._cal = load_balance_cal(self.config.vol_path,
                                             self.config.cal_type)
                self._cal_path = self.config.vol_path
            except Exception:                          # noqa: BLE001
                self._cal = None
        return self._cal

    # ── ingest a written point ───────────────────────────────────────────
    def add_point(self, path) -> None:
        try:
            data = read_point(path)
        except Exception:                              # noqa: BLE001
            return
        row = self._reduce(data)
        if row is None:
            return
        self._rows.append(row)
        self._redraw()

    def _reduce(self, data) -> Optional[Dict[str, float]]:
        attrs = data.get("attrs", {})
        groups = data.get("groups", {})
        alpha = _as_float(attrs.get("alpha"))
        beta = _as_float(attrs.get("beta")) or 0.0
        if alpha is None:
            pos = groups.get("Positioner", {})
            if "Alpha" in pos and len(pos["Alpha"]):
                alpha = float(np.mean(pos["Alpha"]))
        if alpha is None:
            return None
        direction = str(attrs.get("sweep_dir", "") or "")

        # dynamic pressure: prefer the recorded derived value (psi)
        q = None
        tun = groups.get("Tunnel", {})
        if "q_meas" in tun and len(tun["q_meas"]):
            q = float(np.mean(tun["q_meas"]))
        if q is None:
            q = self._q_live_psi()

        # balance group: the file's own balance_group marker when present
        # (self-describing files), else the known group names
        bal_group = str(attrs.get("balance_group", "") or "")
        bal = groups.get(bal_group) if bal_group else None
        if not bal:
            bal = (groups.get("StrainBook_0")
                   or groups.get("ATE_Balance") or {})
        if not bal:
            return None
        geom = Geometry(self.config.ref_area, self.config.ref_chord,
                        self.config.ref_span)

        cal = self._ensure_cal()
        mode = str(attrs.get("mode", self.manager.mode))
        # external-balance files hold resolved wind loads directly (the
        # balance_type marker says so; legacy mode-2 files lack it)
        resolved = (str(attrs.get("balance_type", "")) == "external"
                    or mode == "mode2")
        if cal is not None and not resolved:
            from ..aero import compute_aero
            raw = {k: np.asarray(v, dtype=float) for k, v in bal.items()}
            try:
                res = compute_aero(raw, cal, alpha, beta,
                                   self.config.balance_config, q=q, geom=geom)
            except Exception:                          # noqa: BLE001
                return None
            m = res.means()
            lift, drag = m.get("Lift"), m.get("Drag")
            pitch = m.get("Pitch")
            cl, cd = m.get("CL"), m.get("CD")
        else:
            # resolved-load file (external balance): read wind loads direct
            # (true names; legacy alias fallback for old mode-2 files)
            def _mean(name):
                arr = bal.get(name)
                if arr is None:
                    arr = bal.get(_LEGACY_ATE_ALIAS.get(name, name))
                return float(np.mean(arr)) if arr is not None and len(arr) \
                    else None
            lift, drag, pitch = _mean("Lift"), _mean("Drag"), _mean("Pitch")
            qS = (q or 0.0) * geom.S
            cl = lift / qS if lift is not None and qS > 0 else None
            cd = drag / qS if drag is not None and qS > 0 else None

        return {"alpha": alpha, "beta": beta, "dir": direction,
                "Lift": lift, "Drag": drag, "Pitch": pitch,
                "CL": cl, "CD": cd,
                "L/D": (lift / drag if lift is not None and drag not in
                        (None, 0.0) else None)}

    # ── plotting ─────────────────────────────────────────────────────────
    def _redraw(self) -> None:
        metric = self.metric.currentText()
        self._p_metric.setLabel("left", metric)
        self.count_lbl.setText(f"{len(self._rows)} point"
                               + ("s" if len(self._rows) != 1 else ""))

        def series(direction):
            pts = [(r["alpha"], r[metric]) for r in self._rows
                   if r.get(metric) is not None
                   and (direction is None or r.get("dir") == direction
                        or (direction == "up" and r.get("dir") in ("", "up")))]
            pts.sort(key=lambda t: t[0])
            if not pts:
                return [], []
            xs, ys = zip(*pts)
            return list(xs), list(ys)

        # split up/down legs so hysteresis loops are visible
        has_dn = any(r.get("dir") == "dn" for r in self._rows)
        if has_dn:
            self._scatter_up.setData(*series("up"))
            self._scatter_dn.setData(*series("dn"))
        else:
            self._scatter_up.setData(*series(None))
            self._scatter_dn.setData([], [])

        amap = [(r["alpha"], r["beta"]) for r in self._rows]
        if amap:
            xs, ys = zip(*amap)
            self._map_scatter.setData(list(xs), list(ys))
        else:
            self._map_scatter.setData([], [])

    def shutdown(self) -> None:
        pass


def _as_float(v) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None
