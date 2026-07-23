"""Center live monitors — pyqtgraph strip charts in physical units ONLY.

Tabs: Tunnel (the merged dashboard in :mod:`.tunnel_dashboard` — gauge,
stat tiles, status lights + the Mach/q/RPM strip charts), Balance (raw
channels of the balance-role device), Position (alpha/beta actual vs
target), plus the self-timed Forces and Results panels.

DISPLAY ONLY: no coefficients, no calibration — team directive. History
lives in plain deques (~120 s at the 5 Hz UI sampling rate).
"""

from __future__ import annotations

import math
import time
from collections import deque
from typing import Dict, Optional

from PyQt6.QtCore import QTimer, Qt
from PyQt6.QtWidgets import QMainWindow, QMenu, QTabWidget

from .. import theme
from ..hal import Streaming
from ..manager import DeviceManager

SAMPLE_MS = 200                       # 5 Hz UI sampling of latest()
HISTORY_N = 600                       # ≈120 s of history


class DetachedTabWindow(QMainWindow):
    """Top-level host for a detached monitor tab (multi-monitor use).

    A NORMAL window — title bar with minimize/maximize/close — so it can
    fill a second display. The tab's widget is REPARENTED in as the
    central widget; closing the window re-docks it at its original
    index in the MonitorPanel."""

    def __init__(self, panel: "MonitorPanel", widget, title: str,
                 index: int):
        super().__init__()
        self._panel = panel
        self.tab_title = title
        self.home_index = index
        self.setWindowTitle(f"{title} — Freestream")
        self.setCentralWidget(widget)
        # a NON-current tab page was explicitly hidden by the tab stack —
        # that hidden flag survives reparenting, so re-show it here
        widget.show()
        self.resize(960, 640)

    def closeEvent(self, event) -> None:               # noqa: N802
        self._panel._redock(self)
        super().closeEvent(event)


class MonitorPanel(QTabWidget):
    """Tabbed strip charts; polls latest()/positions()/readback()."""

    def __init__(self, manager: DeviceManager, config=None, parent=None):
        super().__init__(parent)
        theme.apply_pyqtgraph_theme()
        import pyqtgraph as pg
        self._pg = pg

        self.manager = manager
        self.config = config
        self._active = False          # set by the main window on connect
        self._subpanels = []          # self-timed child panels (fwd active)
        self._t0 = time.monotonic()
        self._hist: Dict[str, deque] = {}
        self._targets = {"alpha": None, "beta": None}
        self._balance: Optional[Streaming] = None
        self._bal_curves: Dict[str, object] = {}

        # ── Balance tab: bridge channels on the main plot + a slim
        # excitation strip below (like the StrainBook app) — 10 V of
        # excitation on the same axis would flatten the µV bridges ───────
        from PyQt6.QtWidgets import QVBoxLayout, QWidget
        self._bal_container = QWidget()
        _bal_lay = QVBoxLayout(self._bal_container)
        _bal_lay.setContentsMargins(0, 0, 0, 0)
        _bal_lay.setSpacing(2)
        self._bal_plot = pg.PlotWidget()
        self._bal_plot.showGrid(x=True, y=True, alpha=0.25)
        self._bal_plot.setLabel("left", "bridge signal")
        self._bal_plot.getPlotItem().getAxis("bottom") \
            .setStyle(showValues=False)
        self._bal_legend = self._bal_plot.addLegend(
            offset=(10, 10), labelTextColor=theme.TEXT_DIM)
        _bal_lay.addWidget(self._bal_plot, 4)
        self._bal_exc_plot = pg.PlotWidget()
        self._bal_exc_plot.showGrid(x=True, y=True, alpha=0.25)
        self._bal_exc_plot.setLabel("left", "Exc  (V)")
        self._bal_exc_plot.setLabel("bottom", "t [s]")
        self._bal_exc_plot.setXLink(self._bal_plot)
        _bal_lay.addWidget(self._bal_exc_plot, 1)
        self.addTab(self._bal_container, "Balance")

        # ── Position tab: positioner axes actual vs target (curves are
        # rebuilt per registry — alpha/beta for the sting rigs, x/y/z for
        # the Mode-3 traverse) ────────────────────────────────────────────
        self._pos_plot = pg.PlotWidget()
        self._pos_plot.showGrid(x=True, y=True, alpha=0.25)
        self._pos_plot.setLabel("left", "position")
        self._pos_plot.setLabel("bottom", "t [s]")
        self._pos_legend = self._pos_plot.addLegend(
            offset=(10, 10), labelTextColor=theme.TEXT_DIM)
        self._pos_curves = {}
        self._pos_axes = []           # axis names of the active positioner
        self.addTab(self._pos_plot, "Position")

        self._build_subpanels()
        self._discover()

        # ── detachable tabs (multi-monitor): double-click a tab or use
        # the tab bar's context menu to float it as a real window ────────
        self._detached: Dict[str, DetachedTabWindow] = {}
        self.tabBarDoubleClicked.connect(self.detach_tab)
        bar = self.tabBar()
        bar.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        bar.customContextMenuRequested.connect(self._tab_context_menu)

        self._timer = QTimer(self)
        self._timer.setInterval(SAMPLE_MS)
        self._timer.timeout.connect(self._sample)
        self._timer.start()

    # ── detach / re-dock ─────────────────────────────────────────────────
    def _tab_context_menu(self, pos) -> None:
        index = self.tabBar().tabAt(pos)
        if index < 0:
            return
        menu = QMenu(self)
        act = menu.addAction(f"Detach “{self.tabText(index)}” "
                             "into its own window")
        act.triggered.connect(lambda _c=False, i=index: self.detach_tab(i))
        menu.exec(self.tabBar().mapToGlobal(pos))

    def detach_tab(self, index: int) -> None:
        """Float the tab at ``index`` as its own top-level window.

        The page widget is reparented, NEVER rebuilt — every curve/deque
        reference stays valid, so the panel's ``_sample`` loop (and the
        self-timed subpanels' own timers) keep updating it while
        detached. Closing the floating window re-docks it."""
        if index < 0 or index >= self.count():
            return
        title = self.tabText(index)
        widget = self.widget(index)
        self.removeTab(index)
        win = DetachedTabWindow(self, widget, title, index)
        self._detached[title] = win
        win.show()
        if self.count():
            self.setCurrentIndex(min(index, self.count() - 1))

    def _redock(self, win: DetachedTabWindow) -> None:
        """Floating window closed → tab returns at its original index."""
        self._detached.pop(win.tab_title, None)
        widget = win.takeCentralWidget()
        if widget is None:
            return
        index = min(win.home_index, self.count())
        self.insertTab(index, widget, win.tab_title)
        self.setCurrentIndex(index)

    def redock_all(self) -> None:
        """Close every floating tab window (each close re-docks)."""
        for win in list(self._detached.values()):
            win.close()

    # ── sub-panels (self-timed tabs: Tunnel, Forces, Results) ────────────
    def _build_subpanels(self) -> None:
        from .forces import ForcesPanel
        from .results import ResultsPanel
        from .tunnel_dashboard import TunnelDashboard
        self.tunnel_dash = TunnelDashboard(self.manager, self.config)
        self.insertTab(0, self.tunnel_dash, "Tunnel")
        self.forces = ForcesPanel(self.manager, self.config)
        self.addTab(self.forces, "Forces")
        self.results = ResultsPanel(self.manager, self.config)
        self.addTab(self.results, "Results")
        self._subpanels = [self.tunnel_dash, self.forces, self.results]
        self.setCurrentIndex(0)

    @property
    def active(self) -> bool:
        return self._active

    @active.setter
    def active(self, value: bool) -> None:
        self._active = bool(value)
        for panel in self._subpanels:
            panel.active = bool(value)

    # ── registry sync ────────────────────────────────────────────────────
    def set_manager(self, manager: DeviceManager) -> None:
        self.manager = manager
        self._hist.clear()
        self._t0 = time.monotonic()
        self._discover()
        for panel in self._subpanels:
            panel.set_manager(manager)

    def set_targets(self, alpha: Optional[float],
                    beta: Optional[float]) -> None:
        self._targets["alpha"] = alpha
        self._targets["beta"] = beta
        # forward to the Tunnel dashboard's α/β attitude pad (ghost marker)
        self.tunnel_dash.set_targets(alpha, beta)

    def _discover(self) -> None:
        """Find the balance-role streaming device."""
        bal = self.manager.by_role("balance")
        self._balance = bal if isinstance(bal, Streaming) else None
        # rebuild balance curves from the device's channel list
        for curve, plot in self._bal_curves.values():
            plot.removeItem(curve)
        self._bal_legend.clear()
        self._bal_curves.clear()
        if self._balance is not None:
            try:
                chans = self._balance.channels()
            except Exception:                          # noqa: BLE001
                chans = []
            for i, ch in enumerate(chans):
                # position channels (the ATE's Alpha/Beta stream) belong
                # on the Position tab, not the balance plot
                if getattr(ch, "kind", "") == "position":
                    continue
                # excitation rides the slim strip below the bridge plot —
                # its 10 V would flatten the µV bridge scale
                exc = "excitation" in ch.name.lower()
                plot = self._bal_exc_plot if exc else self._bal_plot
                # width-1 non-AA pen: wider AA streaming pens hit Qt's slow
                # path stroker when repainting embedded (see strainbook app)
                curve = plot.plot(
                    pen=self._pg.mkPen(theme.series_color(i), width=1),
                    antialias=False,
                    **({} if exc else
                       {"name": f"{ch.name} [{ch.unit}]"}))
                self._bal_curves[ch.name] = (curve, plot)
        # no excitation channel (e.g. the ATE's resolved Lift/Drag/… set)
        # → hide the empty excitation strip instead of showing a dead plot
        has_exc = any(plot is self._bal_exc_plot
                      for _curve, plot in self._bal_curves.values())
        self._bal_exc_plot.setVisible(has_exc)
        self._rebuild_position_curves()

    def _rebuild_position_curves(self) -> None:
        """Position-tab curves follow the ACTIVE positioner's axes
        (alpha/beta in deg, or the traverse x/y/z in inches)."""
        for curve in self._pos_curves.values():
            self._pos_plot.removeItem(curve)
        self._pos_legend.clear()
        self._pos_curves.clear()
        self._pos_axes = []
        pos = self.manager.positioner
        if pos is None:
            return
        try:
            specs = list(pos.axes())
        except Exception:                              # noqa: BLE001
            return
        units = sorted({a.unit for a in specs})
        self._pos_plot.setLabel(
            "left", "position [" + "/".join(units) + "]" if units
            else "position")
        for i, spec in enumerate(specs):
            ax = spec.name
            self._pos_axes.append(ax)
            color = theme.series_color(i)
            self._pos_curves[f"pos:{ax}"] = self._pos_plot.plot(
                pen=self._pg.mkPen(color, width=1), antialias=False,
                name=f"{ax} actual [{spec.unit}]")
            # target ghosts exist only for the swept attitude axes today
            if ax in ("alpha", "beta"):
                self._pos_curves[f"tgt:{ax}"] = self._pos_plot.plot(
                    pen=self._pg.mkPen(color, width=1,
                                       style=Qt.PenStyle.DashLine),
                    name=f"{ax} target", connect="finite")

    # ── sampling ─────────────────────────────────────────────────────────
    def _push(self, key: str, t: float, v: float) -> None:
        self._hist.setdefault(key, deque(maxlen=HISTORY_N)).append((t, v))

    def _sample(self) -> None:
        if not self.active:
            return
        t = time.monotonic() - self._t0
        if self._balance is not None and self._bal_curves:
            try:
                for name, v in self._balance.latest().items():
                    if name in self._bal_curves:
                        self._push(f"bal:{name}", t, float(v))
            except Exception:                          # noqa: BLE001
                pass
        pos = self.manager.positioner
        if pos is not None:
            try:
                pp = pos.positions()
                for ax in self._pos_axes:
                    if ax in pp:
                        self._push(f"pos:{ax}", t, float(pp[ax]))
                        if f"tgt:{ax}" in self._pos_curves:
                            tgt = self._targets.get(ax)
                            self._push(f"tgt:{ax}", t,
                                       float(tgt) if tgt is not None
                                       else math.nan)
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

        for name, (curve, _plot) in self._bal_curves.items():
            curve.setData(*data(f"bal:{name}"))
        for key, curve in self._pos_curves.items():
            curve.setData(*data(key), connect="finite")

    def point_done(self, path) -> None:
        """Forward a freshly written point to the Results panel."""
        self.results.add_point(path)

    def shutdown(self) -> None:
        self._timer.stop()
        self.redock_all()             # no floating windows may outlive us
        for panel in self._subpanels:
            panel.shutdown()
