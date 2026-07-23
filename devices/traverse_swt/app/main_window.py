"""Main window + coordinator panel for the SSWT traverse app.

Layout: connection bar → three axis cards (X | Y | Z) + E-STOP column →
position time-history. Tabs: Motion, Diagnostics (raw ControlWord +
750-673 module status/event log), Calibration. Positioning is a
calibrated ``move_to``; the operator moves the stage from the physical
console when uncalibrated.
"""

from __future__ import annotations

import logging
import webbrowser
from pathlib import Path
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDoubleSpinBox, QFileDialog, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton,
    QStatusBar, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
    QWidget,
)

from traverse_swt import about, theme
from traverse_swt.config import TraverseConfig, defaults_path
from traverse_swt.device import TraverseDrive

from .cal_panel import CalibrationPanel
from .diag_panel import DiagnosticsPanel
from .settings_dialog import SettingsDialog

log = logging.getLogger(__name__)

theme.apply_pyqtgraph_theme()

_MAX_PLOT_BINS = 1200
_AXIS_TITLES = {"X": "X — Axial", "Y": "Y — Lateral", "Z": "Z — Vertical"}


def _envelope(x: np.ndarray, y: np.ndarray, max_bins: int = _MAX_PLOT_BINS):
    n = x.size
    if n <= max_bins * 2:
        return x, y
    stride = n // max_bins
    m = (n // stride) * stride
    yb = y[:m].reshape(-1, stride)
    xs = np.repeat(x[:m:stride], 2)
    ys = np.empty(xs.size, dtype=y.dtype)
    ys[0::2] = yb.min(axis=1)
    ys[1::2] = yb.max(axis=1)
    return xs, ys


class _AxisCard(QGroupBox):
    """Readout + motion controls for one traverse axis.

    Uncalibrated axes show raw COUNTS as the primary readout (move the
    stage from the physical console); the inches target/Move unlocks once
    calibrated. Each card carries an emphatic per-axis STOP.
    """

    def __init__(self, name: str, color: str, parent=None):
        super().__init__(_AXIS_TITLES.get(name, name), parent)
        self.axis_name = name
        self._calibrated = False
        g = QGridLayout(self)

        chip = QLabel()
        chip.setFixedSize(10, 10)
        chip.setStyleSheet(f"background-color: {color}; border-radius: 5px;")
        g.addWidget(chip, 0, 0)
        self.big_lbl = QLabel("--")
        self.big_lbl.setStyleSheet(
            "font-family: 'Segoe UI'; font-size: 24pt; font-weight: 600; "
            f"color: {theme.TEXT};")
        g.addWidget(self.big_lbl, 0, 1, 1, 2)
        self.unit_lbl = QLabel("counts")
        self.unit_lbl.setObjectName("dim")
        g.addWidget(self.unit_lbl, 0, 3)

        self.sub_lbl = QLabel("--")
        self.sub_lbl.setObjectName("dim")
        self.sub_lbl.setProperty("mono", "true")
        g.addWidget(self.sub_lbl, 1, 1, 1, 2)

        self.state_lbl = QLabel("idle")
        self.state_lbl.setProperty("mono", "true")
        g.addWidget(self.state_lbl, 1, 3)

        g.addWidget(QLabel("Target"), 2, 0)
        self.target = QDoubleSpinBox()
        self.target.setDecimals(3)
        self.target.setSingleStep(0.1)
        self.target.setSuffix('"')
        g.addWidget(self.target, 2, 1)
        self.move_btn = QPushButton("Move")
        self.move_btn.setObjectName("primary")
        g.addWidget(self.move_btn, 2, 2)
        self.home_btn = QPushButton("Home")
        self.home_btn.setToolTip(
            "Host-side homing: jog to the NEGATIVE limit switch "
            "(StatusWord bit), back off until it clears + a margin, "
            "then calibrate the offset so the limit reads the datum "
            "(default −18\"). Per-power-cycle — re-home each setup.")
        g.addWidget(self.home_btn, 2, 3)

        # emphatic per-axis STOP (filled danger "stop-sign")
        self.stop_btn = QPushButton("■  STOP")
        self.stop_btn.setObjectName("stopaxis")
        self.stop_btn.setToolTip(f"Stop the {name} axis immediately")
        g.addWidget(self.stop_btn, 3, 0, 1, 4)

        foot = QHBoxLayout()
        self.home_lbl = QLabel("")
        self.home_lbl.setObjectName("dim")
        self.home_lbl.setProperty("mono", "true")
        self.home_lbl.setToolTip("Host-side homing state of this axis")
        foot.addWidget(self.home_lbl)
        foot.addStretch(1)
        self.mod_lbl = QLabel("module --")
        self.mod_lbl.setObjectName("dim")
        self.mod_lbl.setProperty("mono", "true")
        self.mod_lbl.setToolTip("750-673 status bytes S1·S2·S3 (raw). "
                                "Transitions are logged in Diagnostics — "
                                "watch S1 when a start faults.")
        foot.addWidget(self.mod_lbl)
        g.addLayout(foot, 4, 0, 1, 4)

    def set_state(self, st: dict):
        self._calibrated = st["calibrated"]
        if self._calibrated:
            self.big_lbl.setText(f"{st['inches']:+8.3f}")
            self.unit_lbl.setText("in")
            self.sub_lbl.setText(f"cnt {st['counts']:+d}")
        else:
            self.big_lbl.setText(f"{st['counts']:+d}")
            self.unit_lbl.setText("counts")
            self.sub_lbl.setText("UNCALIBRATED — move from console")
        self.move_btn.setToolTip(
            "" if self._calibrated
            else "Calibrate the axis to enable position moves")

        s1, s2, s3 = st["module_status"]
        self.mod_lbl.setText(f"module {s1:02X}·{s2:02X}·{s3:02X}")

        if st.get("fault"):                    # e.g. LIMIT trip
            self.state_lbl.setText(st["fault"])
            self.state_lbl.setStyleSheet(f"color: {theme.ERROR};")
        elif st.get("homing"):
            self.state_lbl.setText(f"HOME {st.get('home_state', '')}")
            self.state_lbl.setStyleSheet(f"color: {theme.WARNING};")
        elif st.get("limit"):                  # on the switch, stopped
            self.state_lbl.setText("LIMIT")
            self.state_lbl.setStyleSheet(f"color: {theme.WARNING};")
        elif st["moving"] and st["target"] is not None:
            self.state_lbl.setText(f"→ {st['target']:+.3f}\"")
            self.state_lbl.setStyleSheet(f"color: {theme.WARNING};")
        else:
            self.state_lbl.setText("idle")
            self.state_lbl.setStyleSheet(f"color: {theme.TEXT_DIM};")

        if st.get("homing"):
            self.home_lbl.setText("homing…")
            self.home_lbl.setStyleSheet(f"color: {theme.WARNING};")
        elif st.get("homed"):
            self.home_lbl.setText("homed")
            self.home_lbl.setStyleSheet(f"color: {theme.SUCCESS};")
        else:
            self.home_lbl.setText("")

    def set_motion_enabled(self, connected: bool, axis_enabled: bool = True,
                           home_enabled: bool = True):
        on = connected and axis_enabled
        self.move_btn.setEnabled(on and self._calibrated)
        self.target.setEnabled(on and self._calibrated)
        self.stop_btn.setEnabled(on)
        self.home_btn.setEnabled(on and home_enabled)
        if not home_enabled:
            self.home_btn.setToolTip(
                f"no homing on {self.axis_name} — this axis has no "
                f"homing sequence (home_enabled is off)")

    def set_limits(self, lo: float, hi: float):
        self.target.setRange(lo, hi)


class TraversePanel(QWidget):
    """The complete traverse GUI (also embeddable in host suites).

    ``drive``/``embedded`` support hosting the EXACT same panel inside
    Freestream: pass the host's live :class:`TraverseDrive` so only ONE
    drive/Modbus connection ever exists, and ``embedded=True`` to hide
    the Connection row (the host owns connect/disconnect). With the
    defaults the standalone app behaviour is unchanged — the panel builds
    and owns its own drive.
    """

    statusSignal = pyqtSignal(str)
    moduleSignal = pyqtSignal(object)    # module S1 transition tuple

    def __init__(self, cfg: Optional[TraverseConfig] = None, parent=None,
                 *, drive: Optional[TraverseDrive] = None,
                 embedded: bool = False):
        super().__init__(parent)
        self.setObjectName("root")
        self._embedded = bool(embedded)
        if drive is not None:
            self.device = drive
            self.config = cfg if cfg is not None else drive.config
        else:
            self.config = cfg or TraverseConfig()
            self.device = TraverseDrive(self.config)

        self._build_ui()
        # remember the host's callbacks (embedded: usually None — the
        # adapter polls) so detach() can hand the drive back untouched
        # when the hosting dialog closes.
        self._prev_callbacks = (self.device.on_status,
                                self.device.on_module_status)
        if not self._embedded:
            # standalone: pipe driver status to the main-window status bar.
            # Embedded hosts keep their own on_status wiring — grabbing it
            # here would leave a dangling callback into a deleted panel
            # after the host closes the containing dialog.
            self.device.on_status = self.statusSignal.emit
        # module events feed the Diagnostics tab in BOTH modes (that tab
        # is the whole point of embedding the full panel); embedded hosts
        # must call detach() when done with the panel.
        self.device.on_module_status = self.moduleSignal.emit
        self.moduleSignal.connect(
            lambda ev: self.diag_panel.append_module(ev))

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(100)
        self._ui_timer.timeout.connect(self._refresh_ui)
        self._ui_timer.start()

        self._last_connected = self.device.connected
        if self._last_connected:                # embedded, already-live host
            self._apply_limits()
        self._set_connected_ui(self._last_connected)

    def detach(self):
        """Restore the drive callbacks captured at construction (embedded
        hosts call this when the containing dialog closes, so no callback
        keeps pointing into a deleted panel)."""
        (self.device.on_status,
         self.device.on_module_status) = self._prev_callbacks

    # ── UI ──
    def _build_ui(self):
        self.setStyleSheet(theme.get_stylesheet())
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        conn = self.conn_group = QGroupBox("Connection")
        cl = QHBoxLayout(conn)
        cl.setSpacing(8)
        cl.addWidget(QLabel("WAGO PLC IP"))
        self.ip_edit = QLineEdit(self.config.ip)
        self.ip_edit.setFixedWidth(120)
        cl.addWidget(self.ip_edit)
        self.sim = QCheckBox("Simulate")
        self.sim.setChecked(self.config.force_sim)
        cl.addWidget(self.sim)
        cl.addStretch(1)
        self.lamp = QLabel("DISCONNECTED")
        self.lamp.setProperty("mono", "true")
        self.lamp.setStyleSheet(f"color: {theme.TEXT_DIM}; "
                                f"font-weight: bold;")
        cl.addWidget(self.lamp)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.clicked.connect(self._handle_connect)
        cl.addWidget(self.connect_btn)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self._handle_disconnect)
        cl.addWidget(self.disconnect_btn)
        self.defaults_btn = QPushButton("Set as Defaults")
        self.defaults_btn.setToolTip(
            "Save the CURRENT settings (including live calibration) as "
            "the startup defaults — auto-loaded at every launch. "
            "Separate from File → Save/Load config files.")
        self.defaults_btn.clicked.connect(self.save_defaults)
        cl.addWidget(self.defaults_btn)
        root.addWidget(conn)
        if self._embedded:                  # host owns connect/disconnect
            conn.hide()

        self.tabs = QTabWidget()
        motion = QWidget()
        ml = QVBoxLayout(motion)
        ml.setSpacing(8)

        cards = QHBoxLayout()
        self.cards = {}
        for i, name in enumerate("XYZ"):
            card = _AxisCard(name, theme.series_color(i))
            card.move_btn.clicked.connect(
                lambda _=False, n=name, c=card:
                self._move(n, c.target.value()))
            card.stop_btn.clicked.connect(
                lambda _=False, n=name: self.device.stop_axis(n))
            card.home_btn.clicked.connect(
                lambda _=False, n=name: self._home(n))
            self.cards[name] = card
            cards.addWidget(card, 2)

        side = QGroupBox("All axes")
        sg = QVBoxLayout(side)
        self.estop_btn = QPushButton("■  E-STOP")
        # same danger "stop-sign" family as the per-axis STOPs, but the
        # tallest/most-dominant member (it stops everything)
        self.estop_btn.setObjectName("stopaxis")
        self.estop_btn.setMinimumHeight(76)
        self.estop_btn.setStyleSheet("font-size: 15pt;")
        self.estop_btn.clicked.connect(self.device.stop_all)
        sg.addWidget(self.estop_btn)
        self.stop_all_btn = QPushButton("Stop all")
        self.stop_all_btn.clicked.connect(self.device.stop_all)
        sg.addWidget(self.stop_all_btn)
        sg.addStretch(1)
        note = QLabel("Uncalibrated axes are\nmoved from the physical\n"
                      "console; calibrate to\nenable Move.")
        note.setObjectName("dim")
        sg.addWidget(note)
        cards.addWidget(side, 1)
        ml.addLayout(cards)

        # position history
        self.plot = pg.PlotWidget()
        pi = self.plot.getPlotItem()
        pi.showGrid(x=False, y=True, alpha=0.25)
        pi.setMenuEnabled(False)
        pi.setMouseEnabled(x=False, y=True)
        pi.setClipToView(True)
        for side_ax in ("left", "bottom"):
            ax = pi.getAxis(side_ax)
            ax.setPen(pg.mkPen(theme.AXIS, width=1))
            ax.setTextPen(theme.TEXT_DIM)
            ax.enableAutoSIPrefix(False)
        pi.setLabel("left", "position  (counts)")
        pi.setLabel("bottom", "time before now  (s)")
        pi.addLegend(offset=(8, 8), labelTextColor=theme.TEXT,
                     brush=pg.mkBrush(theme.PLOT_BG + "cc"),
                     pen=pg.mkPen(theme.BORDER))
        self._curves = {
            name: pi.plot([], [], name=name,
                          pen=pg.mkPen(theme.series_color(i), width=2))
            for i, name in enumerate("XYZ")
        }
        ml.addWidget(self.plot, 1)
        self.tabs.addTab(motion, "Motion")

        self.diag_panel = DiagnosticsPanel(self.config, self.device)
        self.tabs.addTab(self.diag_panel, "Diagnostics")

        self.cal_panel = CalibrationPanel(self.config, self.device)
        self.tabs.addTab(self.cal_panel, "Calibration")
        root.addWidget(self.tabs, 1)

    # ── actions ──
    def _handle_connect(self):
        self.config.ip = self.ip_edit.text().strip() or self.config.ip
        self.config.force_sim = self.sim.isChecked()
        try:
            self.device.connect()
        except Exception as exc:                       # noqa: BLE001
            self.statusSignal.emit(f"Connect failed: {exc}")
            log.exception("connect failed")
            self.device.disconnect()
            return
        self._apply_limits()
        self._set_connected_ui(True)

    def _handle_disconnect(self):
        self.device.disconnect()
        self._set_connected_ui(False)

    def save_defaults(self):
        """Persist the CURRENT config (live calibration included — the
        config object is live) as the auto-loaded startup defaults."""
        path = defaults_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.config.save(path)
        except OSError as exc:
            self.statusSignal.emit(f"Defaults save FAILED: {exc}")
            log.exception("defaults save failed")
            return
        self.statusSignal.emit(f"Defaults saved — auto-loads at every "
                               f"launch ({path})")

    def _move(self, name: str, value: float):
        try:
            self.device.move_to(**{name.lower(): value})
        except (ValueError, RuntimeError) as exc:
            self.statusSignal.emit(str(exc))

    def _home(self, name: str):
        """Start the host-side homing sequence (non-blocking; the
        drive's state feeds the card's HOME <phase> label while it
        runs)."""
        try:
            self.device.home_axis(name, wait=False)
        except (ValueError, RuntimeError) as exc:
            self.statusSignal.emit(str(exc))

    def _apply_limits(self):
        for name, card in self.cards.items():
            cfg = self.config.axis(name)
            card.set_limits(cfg.min_in, cfg.max_in)

    def _set_connected_ui(self, connected: bool):
        self.connect_btn.setEnabled(not connected)
        self.disconnect_btn.setEnabled(connected)
        for w in (self.ip_edit, self.sim):
            w.setEnabled(not connected)
        for name, card in self.cards.items():
            cfg = self.config.axis(name)
            card._calibrated = cfg.calibrated
            card.set_motion_enabled(connected, cfg.enabled,
                                    cfg.home_enabled)
        self.estop_btn.setEnabled(connected)
        self.stop_all_btn.setEnabled(connected)
        if not connected:
            self._set_lamp("DISCONNECTED", theme.TEXT_DIM)
        elif self.device.sim_mode:
            self._set_lamp("SIMULATION", theme.WARNING)
        else:
            self._set_lamp("LIVE", theme.SUCCESS)

    def _set_lamp(self, text: str, color: str):
        self.lamp.setText(text)
        self.lamp.setStyleSheet(f"color: {color}; font-weight: bold;")

    # ── refresh ──
    def _refresh_ui(self):
        # track connection changes made OUTSIDE the panel's own buttons
        # (embedded hosts, driver watchdog) so the controls come alive /
        # lock down without a Connect click.
        connected = self.device.connected
        if connected != self._last_connected:
            self._last_connected = connected
            if connected:
                self._apply_limits()
            self._set_connected_ui(connected)
        if not connected:
            return
        state = self.device.state()
        for name, card in self.cards.items():
            st = state[name]
            card.set_state(st)
            card.set_motion_enabled(True, st["enabled"],
                                    self.config.axis(name).home_enabled)
        self.diag_panel.refresh(self.device.control_echo, state)
        self.cal_panel.refresh(state)

        window = self.config.plot_window_s
        n = int(window / max(self.config.loop_ms / 1000.0, 0.01) * 1.1) + 2
        data = self.device.ring.tail(n)
        t = data["t"]
        if t.size >= 2 and self.plot.isVisible():
            # until every enabled axis is calibrated, plot raw counts
            enabled = [nm for nm in "XYZ" if state[nm]["enabled"]]
            all_cal = all(state[nm]["calibrated"] for nm in enabled)
            suffix = "" if all_cal else "_cnt"
            self.plot.getPlotItem().setLabel(
                "left", "position  (in)" if all_cal
                else "position  (counts)")
            x = t - t[-1]
            keep = x >= -window
            for name, curve in self._curves.items():
                if name not in enabled:
                    curve.setData([], [])
                    continue
                xd, yd = _envelope(x[keep], data[name + suffix][keep])
                curve.setData(xd, yd)
            self.plot.getPlotItem().setXRange(-window, 0.0, padding=0)

    def shutdown(self):
        try:
            self.device.disconnect()
        except Exception as exc:                       # noqa: BLE001
            log.warning("shutdown: %s", exc)

    def apply_settings(self):
        self._apply_limits()


class _AboutDialog(QDialog):
    """Small dark-themed About box: app name + version, summary, author
    line, compact version-history table."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"About {about.APP_NAME}")
        self.setStyleSheet(theme.get_stylesheet())
        self.setMinimumWidth(560)
        v = QVBoxLayout(self)
        v.setSpacing(8)

        name = QLabel(about.APP_NAME)
        name.setStyleSheet(f"font-size: 16pt; font-weight: 600; "
                           f"color: {theme.TEXT};")
        v.addWidget(name)
        ver = QLabel(f"version {about.__version__}")
        ver.setProperty("mono", "true")
        ver.setStyleSheet(f"color: {theme.ACCENT_LIGHT}; "
                          f"font-weight: bold;")
        v.addWidget(ver)

        summary = QLabel(about.SUMMARY)
        summary.setWordWrap(True)
        summary.setStyleSheet(f"color: {theme.TEXT_DIM};")
        v.addWidget(summary)

        author = QLabel(f"Author: {about.AUTHOR} — {about.CONTACT}")
        author.setProperty("mono", "true")
        v.addWidget(author)

        hist = QTableWidget(len(about.VERSION_HISTORY), 3)
        hist.setHorizontalHeaderLabels(["Version", "Date", "Changes"])
        hist.verticalHeader().setVisible(False)
        hist.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        hist.setWordWrap(True)
        for row, (version, date, text) in enumerate(about.VERSION_HISTORY):
            for col, val in enumerate((version, date, text)):
                hist.setItem(row, col, QTableWidgetItem(val))
        hist.horizontalHeader().setStretchLastSection(True)
        hist.setColumnWidth(0, 70)
        hist.setColumnWidth(1, 90)
        hist.resizeRowsToContents()
        hist.setMinimumHeight(150)
        v.addWidget(hist)

        close = QPushButton("Close")
        close.setObjectName("primary")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        v.addLayout(row)


class TraverseMainWindow(QMainWindow):
    def __init__(self, cfg: Optional[TraverseConfig] = None):
        super().__init__()
        self.setWindowTitle("SSWT Traverse — WAGO PLC")
        self.resize(1080, 780)
        self.setStyleSheet(theme.get_stylesheet())
        self.panel = TraversePanel(cfg, self)
        self.setCentralWidget(self.panel)

        sb = QStatusBar()
        self.setStatusBar(sb)
        self._sb_status = QLabel("Idle")
        self._sb_status.setProperty("mono", "true")
        sb.addPermanentWidget(self._sb_status, 1)
        self.panel.statusSignal.connect(self._sb_status.setText)

        self._build_menus()

    def _build_menus(self):
        m = self.menuBar().addMenu("&File")
        a_settings = QAction("&Settings…", self)
        a_settings.setShortcut("Ctrl+,")
        a_settings.triggered.connect(self._open_settings)
        m.addAction(a_settings)
        m.addSeparator()
        a_save = QAction("Save config…", self)
        a_save.triggered.connect(self._save_cfg)
        m.addAction(a_save)
        a_load = QAction("Load config…", self)
        a_load.triggered.connect(self._load_cfg)
        m.addAction(a_load)
        a_defaults = QAction("Set current as defaults", self)
        a_defaults.setToolTip("Stored settings auto-load at every launch "
                              "— separate from Save/Load config files")
        a_defaults.triggered.connect(self.panel.save_defaults)
        m.addAction(a_defaults)
        m.addSeparator()
        a_quit = QAction("&Quit", self)
        a_quit.triggered.connect(self.close)
        m.addAction(a_quit)

        h = self.menuBar().addMenu("&Help")       # LAST menu
        a_docs = QAction("&Documentation", self)
        a_docs.triggered.connect(self._open_docs)
        h.addAction(a_docs)
        h.addSeparator()
        a_about = QAction(f"&About {about.APP_NAME}", self)
        a_about.triggered.connect(self._show_about)
        h.addAction(a_about)

    def _open_docs(self):
        docs = Path(__file__).resolve().parents[1] / "docs" / "index.html"
        if docs.exists():
            webbrowser.open(docs.resolve().as_uri())
        else:
            self.statusBar().showMessage(
                f"Documentation not found: {docs}", 5000)

    def _show_about(self):
        _AboutDialog(self).exec()

    def _open_settings(self):
        dlg = SettingsDialog(self.panel.config, self,
                             drive=self.panel.device)
        if dlg.exec():
            self.panel.apply_settings()
            self.statusBar().showMessage("Settings applied", 3000)

    def _save_cfg(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save config",
                                              "traverse_config.json",
                                              "JSON (*.json)")
        if path:
            self.panel.config.save(path)

    def _load_cfg(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load config", "",
                                              "JSON (*.json)")
        if path:
            self.panel.config = TraverseConfig.load(path)
            # rebind the drive to the new config so calibration/limits
            # apply immediately (crescent regression lesson)
            self.panel.device.set_config(self.panel.config)
            self.panel.cal_panel.set_config(self.panel.config)
            self.panel.apply_settings()
            self.statusBar().showMessage(f"Loaded {path}", 3000)

    def closeEvent(self, event):
        self.panel.shutdown()
        super().closeEvent(event)
