"""Main window + coordinator panel for the ARC Crescent app.

Layout: connection bar → axis cards (Alpha | Beta | synchronous move +
E-STOP) → angle time-history. A Calibration tab handles the two-point
encoder↔angle calibration per axis; Settings covers loop tuning.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import QButtonGroup
from PyQt6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton, QSpinBox,
    QStatusBar, QTabWidget, QVBoxLayout, QWidget,
)

from ac_delta import theme
from ac_delta.config import CrescentConfig
from ac_delta.device import CrescentDrive

from .cal_panel import CalibrationPanel
from .settings_dialog import SettingsDialog

log = logging.getLogger(__name__)

theme.apply_pyqtgraph_theme()

_MAX_PLOT_BINS = 1200


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
    """Readout + single-axis motion controls for one axis.

    Uncalibrated axes show the raw ENCODER as the primary readout (angles
    would be meaningless) and only allow jogging; the angle target/Move
    unlocks once the axis is calibrated. Jog buttons are hold-to-run
    (press = move, release = stop).
    """

    def __init__(self, name: str, color: str, parent=None):
        super().__init__(f"{name} axis", parent)
        self.axis_name = name
        self._calibrated = False
        g = QGridLayout(self)

        chip = QLabel()
        chip.setFixedSize(10, 10)
        chip.setStyleSheet(f"background-color: {color}; border-radius: 5px;")
        g.addWidget(chip, 0, 0)
        self.big_lbl = QLabel("--")
        self.big_lbl.setStyleSheet(
            "font-family: 'Segoe UI'; font-size: 26pt; font-weight: 600; "
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
        self.target.setDecimals(2)
        self.target.setSingleStep(0.5)
        self.target.setSuffix("°")
        g.addWidget(self.target, 2, 1)
        self.move_btn = QPushButton("Move")
        self.move_btn.setObjectName("primary")
        g.addWidget(self.move_btn, 2, 2)
        self.stop_btn = QPushButton("Stop")
        g.addWidget(self.stop_btn, 2, 3)

        # hold-to-run jog row (big targets; press = move, release = stop)
        jog_row = QHBoxLayout()
        self.jog_minus = QPushButton("−  Jog")
        self.jog_plus = QPushButton("Jog  +")
        for btn in (self.jog_minus, self.jog_plus):
            btn.setMinimumHeight(44)
            btn.setStyleSheet("font-size: 13pt; font-weight: bold;")
            btn.setToolTip("Hold to move, release to stop")
            jog_row.addWidget(btn)
        g.addLayout(jog_row, 3, 0, 1, 4)

        # speed: five fat, single-click selectable steps
        speed_row = QHBoxLayout()
        speed_row.setSpacing(4)
        speed_row.addWidget(QLabel("Speed"))
        self._speed_group = QButtonGroup(self)
        self._speed_group.setExclusive(True)
        self.speed_btns = []
        for step in range(1, 6):
            b = QPushButton(str(step))
            b.setCheckable(True)
            b.setMinimumHeight(34)
            b.setMinimumWidth(40)
            self._speed_group.addButton(b, step)
            self.speed_btns.append(b)
            speed_row.addWidget(b)
        self.speed_btns[1].setChecked(True)          # default step 2
        g.addLayout(speed_row, 4, 0, 1, 4)

    def jog_step(self) -> int:
        return max(1, self._speed_group.checkedId())

    def set_state(self, angle: float, encoder: int, moving: bool,
                  jogging: bool, target, calibrated: bool):
        self._calibrated = calibrated
        if calibrated:
            self.big_lbl.setText(f"{angle:+8.3f}")
            self.unit_lbl.setText("deg")
            self.sub_lbl.setText(f"enc {encoder:+d}")
        else:
            self.big_lbl.setText(f"{encoder:+d}")
            self.unit_lbl.setText("counts")
            self.sub_lbl.setText("UNCALIBRATED — jog only")
        self.move_btn.setToolTip(
            "" if calibrated else "Calibrate the axis to enable angle moves")
        if jogging:
            self.state_lbl.setText("JOG")
            self.state_lbl.setStyleSheet(f"color: {theme.WARNING};")
        elif moving and target is not None:
            self.state_lbl.setText(f"→ {target:+.2f}°")
            self.state_lbl.setStyleSheet(f"color: {theme.WARNING};")
        else:
            self.state_lbl.setText("idle")
            self.state_lbl.setStyleSheet(f"color: {theme.TEXT_DIM};")

    def set_motion_enabled(self, connected: bool):
        self.move_btn.setEnabled(connected and self._calibrated)
        self.target.setEnabled(connected and self._calibrated)
        for w in [self.stop_btn, self.jog_minus, self.jog_plus,
                  *self.speed_btns]:
            w.setEnabled(connected)

    def set_limits(self, lo: float, hi: float):
        self.target.setRange(lo, hi)


class CrescentPanel(QWidget):
    """The complete crescent GUI (also embeddable in host suites).

    ``drive``/``embedded`` support hosting the EXACT same panel inside
    Freestream: pass the host's live :class:`CrescentDrive` so only ONE
    drive/Modbus connection ever exists, and ``embedded=True`` to hide the
    Connection row (the host owns connect/disconnect). With the defaults
    the standalone app behaviour is unchanged — the panel builds and owns
    its own drive.
    """

    statusSignal = pyqtSignal(str)

    def __init__(self, cfg: Optional[CrescentConfig] = None, parent=None,
                 *, drive: Optional[CrescentDrive] = None,
                 embedded: bool = False):
        super().__init__(parent)
        self.setObjectName("root")
        self._embedded = bool(embedded)
        if drive is not None:
            self.device = drive
            self.config = cfg if cfg is not None else drive.config
        else:
            self.config = cfg or CrescentConfig()
            self.device = CrescentDrive(self.config)

        self._build_ui()
        if not self._embedded:
            # standalone: pipe driver status to the main-window status bar.
            # Embedded hosts keep their own on_status wiring — grabbing it
            # here would leave a dangling callback into a deleted panel
            # after the host closes the containing dialog.
            self.device.on_status = self.statusSignal.emit

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(100)
        self._ui_timer.timeout.connect(self._refresh_ui)
        self._ui_timer.start()

        self._last_connected = self.device.connected
        if self._last_connected:                # embedded, already-live host
            self._apply_limits()
        self._set_connected_ui(self._last_connected)

    # ── UI ──
    def _build_ui(self):
        self.setStyleSheet(theme.get_stylesheet())
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        conn = self.conn_group = QGroupBox("Connection")
        cl = QHBoxLayout(conn)
        cl.setSpacing(8)
        cl.addWidget(QLabel("Alpha IP"))
        self.alpha_ip = QLineEdit(self.config.alpha.ip)
        self.alpha_ip.setFixedWidth(120)
        cl.addWidget(self.alpha_ip)
        cl.addWidget(QLabel("Beta IP"))
        self.beta_ip = QLineEdit(self.config.beta.ip)
        self.beta_ip.setFixedWidth(120)
        cl.addWidget(self.beta_ip)
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
        root.addWidget(conn)
        if self._embedded:                  # host owns connect/disconnect
            conn.hide()

        self.tabs = QTabWidget()
        motion = QWidget()
        ml = QVBoxLayout(motion)
        ml.setSpacing(8)

        cards = QHBoxLayout()
        self.alpha_card = _AxisCard("Alpha", theme.series_color(0))
        self.alpha_card.move_btn.clicked.connect(
            lambda: self._move(alpha=self.alpha_card.target.value()))
        self.alpha_card.stop_btn.clicked.connect(
            lambda: self.device.stop_axis("alpha"))
        self._wire_jog(self.alpha_card)
        cards.addWidget(self.alpha_card, 2)

        self.beta_card = _AxisCard("Beta", theme.series_color(1))
        self.beta_card.move_btn.clicked.connect(
            lambda: self._move(beta=self.beta_card.target.value()))
        self.beta_card.stop_btn.clicked.connect(
            lambda: self.device.stop_axis("beta"))
        self._wire_jog(self.beta_card)
        cards.addWidget(self.beta_card, 2)

        sync = QGroupBox("Synchronous move")
        sg = QGridLayout(sync)
        sg.addWidget(QLabel("α"), 0, 0)
        self.sync_alpha = QDoubleSpinBox()
        self.sync_alpha.setDecimals(2)
        self.sync_alpha.setSuffix("°")
        sg.addWidget(self.sync_alpha, 0, 1)
        sg.addWidget(QLabel("β"), 1, 0)
        self.sync_beta = QDoubleSpinBox()
        self.sync_beta.setDecimals(2)
        self.sync_beta.setSuffix("°")
        sg.addWidget(self.sync_beta, 1, 1)
        self.sync_btn = QPushButton("Move Both")
        self.sync_btn.setObjectName("success")
        self.sync_btn.clicked.connect(
            lambda: self._move(alpha=self.sync_alpha.value(),
                               beta=self.sync_beta.value()))
        sg.addWidget(self.sync_btn, 0, 2, 2, 1)

        self.estop_btn = QPushButton("E-STOP")
        self.estop_btn.setObjectName("danger")
        self.estop_btn.setMinimumHeight(56)
        self.estop_btn.clicked.connect(self.device.stop_all)
        sg.addWidget(self.estop_btn, 2, 0, 1, 3)
        cards.addWidget(sync, 1)
        ml.addLayout(cards)

        # angle history
        self.plot = pg.PlotWidget()
        pi = self.plot.getPlotItem()
        pi.showGrid(x=False, y=True, alpha=0.25)
        pi.setMenuEnabled(False)
        pi.setMouseEnabled(x=False, y=True)
        pi.setClipToView(True)
        for side in ("left", "bottom"):
            ax = pi.getAxis(side)
            ax.setPen(pg.mkPen(theme.AXIS, width=1))
            ax.setTextPen(theme.TEXT_DIM)
            ax.enableAutoSIPrefix(False)
        pi.setLabel("left", "angle  (deg)")
        pi.setLabel("bottom", "time before now  (s)")
        pi.addLegend(offset=(8, 8), labelTextColor=theme.TEXT,
                     brush=pg.mkBrush(theme.PLOT_BG + "cc"),
                     pen=pg.mkPen(theme.BORDER))
        self._curves = {
            "Alpha": pi.plot([], [], name="Alpha",
                             pen=pg.mkPen(theme.series_color(0), width=2)),
            "Beta": pi.plot([], [], name="Beta",
                            pen=pg.mkPen(theme.series_color(1), width=2)),
        }
        ml.addWidget(self.plot, 1)
        self.tabs.addTab(motion, "Motion")

        self.cal_panel = CalibrationPanel(self.config, self.device)
        self.tabs.addTab(self.cal_panel, "Calibration")
        root.addWidget(self.tabs, 1)

    # ── actions ──
    def _handle_connect(self):
        self.config.alpha.ip = self.alpha_ip.text().strip() or \
            self.config.alpha.ip
        self.config.beta.ip = self.beta_ip.text().strip() or \
            self.config.beta.ip
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

    def _move(self, alpha=None, beta=None):
        try:
            self.device.move_to(alpha=alpha, beta=beta)
        except (ValueError, RuntimeError) as exc:
            self.statusSignal.emit(str(exc))

    def _wire_jog(self, card: _AxisCard):
        """Hold-to-run: pressed starts the jog, released stops it."""
        name = card.axis_name

        def start(forward: bool):
            try:
                self.device.jog(name, forward=forward,
                                step=card.jog_step())
            except RuntimeError as exc:
                self.statusSignal.emit(str(exc))

        card.jog_plus.pressed.connect(lambda: start(True))
        card.jog_minus.pressed.connect(lambda: start(False))
        card.jog_plus.released.connect(lambda: self.device.jog_stop(name))
        card.jog_minus.released.connect(lambda: self.device.jog_stop(name))

    def _apply_limits(self):
        self.alpha_card.set_limits(self.config.alpha.min_deg,
                                   self.config.alpha.max_deg)
        self.beta_card.set_limits(self.config.beta.min_deg,
                                  self.config.beta.max_deg)
        self.sync_alpha.setRange(self.config.alpha.min_deg,
                                 self.config.alpha.max_deg)
        self.sync_beta.setRange(self.config.beta.min_deg,
                                self.config.beta.max_deg)

    def _set_connected_ui(self, connected: bool):
        self.connect_btn.setEnabled(not connected)
        self.disconnect_btn.setEnabled(connected)
        for w in (self.alpha_ip, self.beta_ip, self.sim):
            w.setEnabled(not connected)
        self.alpha_card._calibrated = self.config.alpha.calibrated
        self.beta_card._calibrated = self.config.beta.calibrated
        self.alpha_card.set_motion_enabled(connected)
        self.beta_card.set_motion_enabled(connected)
        both_cal = (self.config.alpha.calibrated and
                    self.config.beta.calibrated)
        self.sync_btn.setEnabled(connected and both_cal)
        self.estop_btn.setEnabled(connected)
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
        a, b = state["Alpha"], state["Beta"]
        self.alpha_card.set_state(a["angle"], a["encoder"], a["moving"],
                                  a["jogging"], a["target"],
                                  a["calibrated"])
        self.beta_card.set_state(b["angle"], b["encoder"], b["moving"],
                                 b["jogging"], b["target"],
                                 b["calibrated"])
        self.alpha_card.set_motion_enabled(True)
        self.beta_card.set_motion_enabled(True)
        self.sync_btn.setEnabled(a["calibrated"] and b["calibrated"])
        self.cal_panel.refresh(state)

        window = self.config.plot_window_s
        n = int(window / max(self.config.loop_ms / 1000.0, 0.01) * 1.1) + 2
        data = self.device.ring.tail(n)
        t = data["t"]
        if t.size >= 2 and self.plot.isVisible():
            # until both axes are calibrated, plot raw encoder counts
            both_cal = a["calibrated"] and b["calibrated"]
            suffix = "" if both_cal else "_enc"
            self.plot.getPlotItem().setLabel(
                "left", "angle  (deg)" if both_cal else "encoder  (counts)")
            x = t - t[-1]
            keep = x >= -window
            for name, curve in self._curves.items():
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


class CrescentMainWindow(QMainWindow):
    def __init__(self, cfg: Optional[CrescentConfig] = None):
        super().__init__()
        self.setWindowTitle("ARC Crescent — SSWT Sting Drive")
        self.resize(1000, 760)
        self.setStyleSheet(theme.get_stylesheet())
        self.panel = CrescentPanel(cfg, self)
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
        m.addSeparator()
        a_quit = QAction("&Quit", self)
        a_quit.triggered.connect(self.close)
        m.addAction(a_quit)

    def _open_settings(self):
        dlg = SettingsDialog(self.panel.config, self)
        if dlg.exec():
            self.panel.apply_settings()
            self.statusBar().showMessage("Settings applied", 3000)

    def _save_cfg(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save config",
                                              "crescent_config.json",
                                              "JSON (*.json)")
        if path:
            self.panel.config.save(path)

    def _load_cfg(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load config", "",
                                              "JSON (*.json)")
        if path:
            self.panel.config = CrescentConfig.load(path)
            # rebind the drive (axis states + live axes) to the new config
            # so calibration/limits apply immediately
            self.panel.device.set_config(self.panel.config)
            self.panel.cal_panel.set_config(self.panel.config)
            self.panel.apply_settings()
            self.statusBar().showMessage(f"Loaded {path}", 3000)

    def closeEvent(self, event):
        self.panel.shutdown()
        super().closeEvent(event)
