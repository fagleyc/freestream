"""Main window for the LSWT fan-control app (North / South tunnel).

Layout: connection bar (tunnel selector, IP, sim, defaults) → dashboard
(Hz gauge + rotor | stat tiles + ramp progress) → strip charts (actual
Hz + velocity) → ARM-gated control section. Mirrors the tunnel_plc
arming UX: Start/Stop/Apply stay disabled until the operator explicitly
ARMS fan control; the E-STOP is always live and prominent.
"""

from __future__ import annotations

import logging
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QProgressBar, QPushButton, QStatusBar, QVBoxLayout, QWidget,
)

from lswt import calibration, theme
from lswt.config import LswtConfig, defaults_path, load_startup_config
from lswt.device import LswtDrive
from lswt.drive import LswtError

from .settings_dialog import SettingsDialog
from .widgets import FanRotor, HzGauge, StatTile

log = logging.getLogger(__name__)

theme.apply_pyqtgraph_theme()

_MAX_PLOT_BINS = 1200
_UNIT_LABELS = {"fps": "ft/s", "mps": "m/s", "kph": "km/h",
                "mph": "mph", "Mach": "Mach"}


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


class LswtPanel(QWidget):
    """The complete LSWT fan GUI for one tunnel (also embeddable).

    ``device``/``embedded`` support hosting the EXACT same panel inside
    Freestream: pass the host's live :class:`LswtDrive` so only ONE
    Modbus connection ever exists, and ``embedded=True`` to hide the
    Connection row (the host owns connect/disconnect — the panel comes
    alive on its refresh timer when the host connects). The arming
    safety is untouched: Start/Stop/Apply stay disabled until the
    operator ARMS fan control in the panel. With the defaults the
    standalone app behaviour is unchanged — the panel builds and owns
    its own drive.
    """

    statusSignal = pyqtSignal(str)
    tunnelChanged = pyqtSignal(str)      # new window-title label

    def __init__(self, cfg: Optional[LswtConfig] = None, parent=None,
                 *, device: Optional[LswtDrive] = None,
                 embedded: bool = False):
        super().__init__(parent)
        self.setObjectName("root")
        self._embedded = bool(embedded)
        if device is not None:
            self.device = device
            self.config = cfg if cfg is not None else device.config
        else:
            self.config = cfg or LswtConfig.for_tunnel("north")
            self.device = LswtDrive(self.config)
        self._armed = False

        self._build_ui()
        if not self._embedded:
            # standalone: pipe driver status (poll thread!) through the
            # signal so it lands on the GUI thread. Embedded hosts keep
            # their own on_status wiring — grabbing it here would leave a
            # dangling callback into a deleted panel after the host
            # closes the containing dialog.
            self.device.on_status = self.statusSignal.emit

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(100)
        self._ui_timer.timeout.connect(self._refresh_ui)
        self._ui_timer.start()

        self._last_connected = self.device.connected
        self._set_connected_ui(self._last_connected)

    # ── UI ──
    def _build_ui(self):
        self.setStyleSheet(theme.get_stylesheet())
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        conn = self.conn_group = QGroupBox(
            "Connection — ABB ACS530 fan drive (Modbus TCP)")
        cl = QHBoxLayout(conn)
        cl.setSpacing(8)
        cl.addWidget(QLabel("Tunnel"))
        self.tunnel_combo = QComboBox()
        self.tunnel_combo.addItems(["North", "South"])
        self.tunnel_combo.setCurrentIndex(
            0 if self.config.tunnel == "north" else 1)
        self.tunnel_combo.currentIndexChanged.connect(self._switch_tunnel)
        cl.addWidget(self.tunnel_combo)
        cl.addWidget(QLabel("Drive IP"))
        self.ip_edit = QLineEdit(self.config.ip)
        self.ip_edit.setFixedWidth(120)
        self.ip_edit.setToolTip(
            "TODO(Casey): set the real North/South drive IPs — "
            "192.168.0.1 is a placeholder (the C# read them from a "
            "runtime XML not in the source tree)")
        cl.addWidget(self.ip_edit)
        self.sim = QCheckBox("Simulate")
        self.sim.setChecked(self.config.force_sim)
        cl.addWidget(self.sim)
        cl.addStretch(1)
        self.stale_lbl = QLabel("")
        self.stale_lbl.setProperty("mono", "true")
        cl.addWidget(self.stale_lbl)
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
            "Save the CURRENT settings as this tunnel's startup "
            "defaults — auto-loaded at every launch (per-tunnel file). "
            "Separate from File → Save/Load config files.")
        self.defaults_btn.clicked.connect(self.save_defaults)
        cl.addWidget(self.defaults_btn)
        root.addWidget(conn)
        if self._embedded:              # host owns connect/disconnect
            conn.hide()

        # ── dashboard ──
        mid = QHBoxLayout()
        gauge_box = QGroupBox("Fan speed")
        gg = QGridLayout(gauge_box)
        gg.setContentsMargins(6, 4, 6, 4)
        self.rotor = FanRotor()
        gg.addWidget(self.rotor, 0, 0,
                     alignment=pg.QtCore.Qt.AlignmentFlag.AlignLeft |
                     pg.QtCore.Qt.AlignmentFlag.AlignTop)
        self.gauge = HzGauge()
        self.gauge.setMaximumHeight(330)
        gg.addWidget(self.gauge, 0, 0, 2, 2)
        gg.setRowStretch(1, 1)
        gg.setColumnStretch(1, 1)
        gauge_box.setMaximumHeight(380)
        mid.addWidget(gauge_box, 3)

        flow_box = QGroupBox("Flow")
        fg = QGridLayout(flow_box)
        fg.setContentsMargins(10, 6, 10, 6)
        self.tile_hz = StatTile("Actual frequency", "Hz")
        fg.addWidget(self.tile_hz, 0, 0)
        self.tile_vel = StatTile("Tunnel velocity", "ft/s")
        fg.addWidget(self.tile_vel, 0, 1)
        self.tile_set = StatTile("Setpoint", "Hz")
        fg.addWidget(self.tile_set, 1, 0)
        self.tile_cmd = StatTile("Commanded (ramped)", "Hz")
        self.tile_cmd.setToolTip(
            "The reference actually written to the drive — ramps toward "
            "the setpoint at the configured Hz/s, never step-jumps")
        fg.addWidget(self.tile_cmd, 1, 1)

        unit_row = QHBoxLayout()
        unit_row.addWidget(QLabel("Velocity unit"))
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(list(calibration.UNITS))
        self.unit_combo.currentTextChanged.connect(self._unit_changed)
        unit_row.addWidget(self.unit_combo)
        unit_row.addStretch(1)
        fg.addLayout(unit_row, 2, 0, 1, 2)

        self.ramp_bar = QProgressBar()
        self.ramp_bar.setRange(0, 1000)
        self.ramp_bar.setTextVisible(False)
        self.ramp_bar.setMaximumHeight(10)
        self.ramp_bar.setToolTip("Ramp progress: actual vs setpoint")
        fg.addWidget(self.ramp_bar, 3, 0, 1, 2)
        self.ramp_lbl = QLabel("")
        self.ramp_lbl.setProperty("mono", "true")
        self.ramp_lbl.setObjectName("dim")
        fg.addWidget(self.ramp_lbl, 4, 0, 1, 2)
        fg.setRowStretch(5, 1)
        flow_box.setMaximumHeight(380)
        mid.addWidget(flow_box, 3)
        root.addLayout(mid, 3)

        # ── strip charts (width-1, non-antialiased streaming pens) ──
        charts = QHBoxLayout()
        self.hz_plot = pg.PlotWidget()
        self._style_plot(self.hz_plot, "fan frequency  (Hz)")
        pi = self.hz_plot.getPlotItem()
        self._hz_curves = {
            "actual_hz": pi.plot([], [], name="Actual",
                                 pen=pg.mkPen(theme.series_color(0),
                                              width=1),
                                 antialias=False),
            "cmd_hz": pi.plot([], [], name="Commanded",
                              pen=pg.mkPen(theme.series_color(2), width=1,
                                           style=pg.QtCore.Qt.PenStyle
                                           .DashLine),
                              antialias=False),
        }
        charts.addWidget(self.hz_plot)
        self.vel_plot = pg.PlotWidget()
        self._style_plot(self.vel_plot, "velocity  (ft/s)")
        self._vel_curve = self.vel_plot.getPlotItem().plot(
            [], [], name="Velocity",
            pen=pg.mkPen(theme.series_color(1), width=1), antialias=False)
        self.vel_plot.setXLink(self.hz_plot)
        charts.addWidget(self.vel_plot)
        root.addLayout(charts, 2)

        root.addWidget(self._build_control_box())

    @staticmethod
    def _style_plot(plot: pg.PlotWidget, ylabel: str):
        pi = plot.getPlotItem()
        pi.showGrid(x=False, y=True, alpha=0.25)
        pi.setMenuEnabled(False)
        pi.setMouseEnabled(x=False, y=True)
        pi.setClipToView(True)
        for side in ("left", "bottom"):
            ax = pi.getAxis(side)
            ax.setPen(pg.mkPen(theme.AXIS, width=1))
            ax.setTextPen(theme.TEXT_DIM)
            ax.enableAutoSIPrefix(False)
        pi.setLabel("left", ylabel)
        pi.setLabel("bottom", "time before now  (s)")
        pi.addLegend(offset=(8, 8), labelTextColor=theme.TEXT,
                     brush=pg.mkBrush(theme.PLOT_BG + "cc"),
                     pen=pg.mkPen(theme.BORDER))
        plot.setMinimumHeight(140)

    def _build_control_box(self) -> QGroupBox:
        self.ctrl_box = QGroupBox("Control — DISARMED")
        cg = QGridLayout(self.ctrl_box)

        self.arm_btn = QPushButton("ARM FAN CONTROL")
        self.arm_btn.setCheckable(True)
        self.arm_btn.setObjectName("danger")
        self.arm_btn.setMinimumHeight(44)
        self.arm_btn.setToolTip(
            "Start/Stop and setpoint commands stay disabled until "
            "armed. The E-STOP is always live.")
        self.arm_btn.clicked.connect(self._handle_arm)
        cg.addWidget(self.arm_btn, 0, 0, 2, 1)

        cg.addWidget(QLabel("Setpoint"), 0, 1)
        self.hz_spin = QDoubleSpinBox()
        self.hz_spin.setRange(0.0, self.config.max_hz)
        self.hz_spin.setDecimals(1)
        self.hz_spin.setSingleStep(0.5)
        self.hz_spin.setSuffix(" Hz")
        self.hz_spin.valueChanged.connect(self._hz_spin_changed)
        cg.addWidget(self.hz_spin, 0, 2)
        self.vel_spin = QDoubleSpinBox()
        self.vel_spin.setDecimals(2)
        self.vel_spin.setSingleStep(1.0)
        self.vel_spin.valueChanged.connect(self._vel_spin_changed)
        cg.addWidget(self.vel_spin, 0, 3)
        self.apply_btn = QPushButton("Apply Setpoint")
        self.apply_btn.setObjectName("primary")
        self.apply_btn.clicked.connect(self._apply_setpoint)
        cg.addWidget(self.apply_btn, 0, 4)

        self.start_btn = QPushButton("Start Fan")
        self.start_btn.setObjectName("success")
        self.start_btn.clicked.connect(self._start_fan)
        cg.addWidget(self.start_btn, 1, 1, 1, 2)
        self.stop_btn = QPushButton("Stop Fan")
        self.stop_btn.clicked.connect(self._stop_fan)
        cg.addWidget(self.stop_btn, 1, 3, 1, 2)

        # E-STOP: ALWAYS enabled (device.estop() is safe in any state)
        self.estop_btn = QPushButton("■  E-STOP")
        self.estop_btn.setObjectName("stopaxis")
        self.estop_btn.setMinimumHeight(64)
        self.estop_btn.setToolTip(
            "Immediate STOP word + zero reference, written from the UI "
            "thread — always live, arming not required")
        self.estop_btn.clicked.connect(self.device_estop)
        cg.addWidget(self.estop_btn, 0, 5, 2, 1)
        cg.setColumnStretch(5, 1)

        self._unit_changed(self.unit_combo.currentText())
        return self.ctrl_box

    # ── setpoint cross-update ──
    def _current_unit(self) -> str:
        return self.unit_combo.currentText()

    def _hz_spin_changed(self, hz: float):
        unit = self._current_unit()
        v = calibration.fps_to_unit(calibration.hz_to_fps(hz), unit)
        self.vel_spin.blockSignals(True)
        self.vel_spin.setValue(v)
        self.vel_spin.blockSignals(False)

    def _vel_spin_changed(self, value: float):
        unit = self._current_unit()
        hz = calibration.fps_to_hz(calibration.unit_to_fps(value, unit))
        self.hz_spin.blockSignals(True)
        self.hz_spin.setValue(min(hz, self.config.max_hz))
        self.hz_spin.blockSignals(False)

    def _unit_changed(self, unit: str):
        label = _UNIT_LABELS.get(unit, unit)
        max_v = calibration.fps_to_unit(calibration.MAX_FPS, unit)
        self.vel_spin.blockSignals(True)
        self.vel_spin.setRange(0.0, max_v)
        self.vel_spin.setDecimals(4 if unit == "Mach" else 2)
        self.vel_spin.setSingleStep(0.005 if unit == "Mach" else 1.0)
        self.vel_spin.setSuffix(f" {label}" if unit != "Mach" else " M")
        self.vel_spin.blockSignals(False)
        self._hz_spin_changed(self.hz_spin.value())
        self.tile_vel.set_unit(label)
        self.vel_plot.getPlotItem().setLabel("left",
                                             f"velocity  ({label})")

    # ── actions ──
    def _switch_tunnel(self, index: int):
        tunnel = "north" if index == 0 else "south"
        if tunnel == self.config.tunnel:
            return
        if self.device.connected:      # combo is disabled connected; guard
            return
        self.config = load_startup_config(tunnel)
        self.device = LswtDrive(self.config)
        self.device.on_status = self.statusSignal.emit
        self.ip_edit.setText(self.config.ip)
        self.sim.setChecked(self.config.force_sim)
        self.hz_spin.setRange(0.0, self.config.max_hz)
        self._set_connected_ui(False)
        self.tunnelChanged.emit(self.config.label)
        self.statusSignal.emit(f"Switched to {self.config.label} "
                               f"({self.config.ip})")

    def _handle_connect(self):
        self.config.ip = self.ip_edit.text().strip() or self.config.ip
        self.config.force_sim = self.sim.isChecked()
        try:
            self.device.connect()
        except Exception as exc:                       # noqa: BLE001
            self.statusSignal.emit(f"Connect failed: {exc}")
            log.exception("connect failed")
            try:
                self.device.disconnect()
            except Exception:                          # noqa: BLE001
                pass
            return
        self._last_connected = True
        self._set_connected_ui(True)

    def _handle_disconnect(self):
        self._disarm()
        self.device.disconnect()
        self._last_connected = False
        self._set_connected_ui(False)

    def _handle_arm(self):
        if not self.arm_btn.isChecked():
            self._disarm()
            return
        if not self.device.connected:
            self.arm_btn.setChecked(False)
            return
        if not self.device.sim_mode:
            confirm = QMessageBox.question(
                self, "Arm fan control?",
                f"This enables commands to the REAL {self.config.label} "
                f"fan:\nstart/stop and speed reference "
                f"(max {self.config.max_hz:g} Hz).\n\nArm fan control?",
                QMessageBox.StandardButton.Yes |
                QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if confirm != QMessageBox.StandardButton.Yes:
                self.arm_btn.setChecked(False)
                return
        self._armed = True
        self._set_armed_ui(True)
        self.statusSignal.emit("Fan control ARMED")

    def _disarm(self):
        self._armed = False
        self.arm_btn.setChecked(False)
        self._set_armed_ui(False)

    def _apply_setpoint(self):
        if not self._armed:
            return
        try:
            self.device.set_hz(self.hz_spin.value())
        except LswtError as exc:
            self.statusSignal.emit(str(exc))

    def _start_fan(self):
        if not self._armed:
            return
        try:
            self.device.set_hz(self.hz_spin.value())
            self.device.fan_start()
        except LswtError as exc:
            self.statusSignal.emit(str(exc))

    def _stop_fan(self):
        try:
            self.device.fan_stop()
        except LswtError as exc:
            self.statusSignal.emit(str(exc))

    def device_estop(self):
        self.device.estop()

    def save_defaults(self):
        """Persist the CURRENT config as this tunnel's startup defaults."""
        self.config.ip = self.ip_edit.text().strip() or self.config.ip
        self.config.force_sim = self.sim.isChecked()
        path = defaults_path(self.config.tunnel)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.config.save(path)
        except OSError as exc:
            self.statusSignal.emit(f"Defaults save FAILED: {exc}")
            log.exception("defaults save failed")
            return
        self.statusSignal.emit(f"Defaults saved for {self.config.label} "
                               f"— auto-loads at every launch ({path})")

    # ── UI state ──
    def _set_connected_ui(self, connected: bool):
        self.connect_btn.setEnabled(not connected)
        self.disconnect_btn.setEnabled(connected)
        for w in (self.ip_edit, self.sim, self.tunnel_combo):
            w.setEnabled(not connected)
        self.arm_btn.setEnabled(connected)
        if not connected:
            self._disarm()
            self._set_lamp("DISCONNECTED", theme.TEXT_DIM)
            self.stale_lbl.setText("")
        elif self.device.sim_mode:
            self._set_lamp("SIMULATION", theme.WARNING)
        else:
            self._set_lamp("LIVE (monitor)", theme.SUCCESS)

    def _set_armed_ui(self, armed: bool):
        self.ctrl_box.setTitle("Control — ARMED" if armed
                               else "Control — DISARMED")
        for w in (self.start_btn, self.stop_btn, self.apply_btn):
            w.setEnabled(armed)
        # E-STOP deliberately untouched: always enabled
        if armed:
            self._set_lamp("SIM + ARMED" if self.device.sim_mode
                           else "LIVE + ARMED", theme.ERROR)
        elif self.device.connected:
            self._set_connected_ui(True)

    def _set_lamp(self, text: str, color: str):
        self.lamp.setText(text)
        self.lamp.setStyleSheet(f"color: {color}; font-weight: bold;")

    # ── refresh ──
    def _refresh_ui(self):
        connected = self.device.connected
        if connected != self._last_connected:
            self._last_connected = connected
            self._set_connected_ui(connected)
        if not connected:
            return
        st = self.device.state()
        unit = self._current_unit()
        label = _UNIT_LABELS.get(unit, unit)

        self.gauge.set_state(st["actual_hz"], st["setpoint_hz"],
                             self.config.max_hz)
        self.rotor.set_state(st["actual_hz"],
                             st["running"] or st["actual_hz"] > 0.05)
        self.tile_hz.set_value(f"{st['actual_hz']:.1f}")
        vel = calibration.fps_to_unit(st["velocity_fps"], unit)
        self.tile_vel.set_value(f"{vel:.4f}" if unit == "Mach"
                                else f"{vel:.1f}")
        self.tile_set.set_value(f"{st['setpoint_hz']:.1f}")
        cmd_color = theme.WARNING if st["ramping"] else None
        self.tile_cmd.set_value(f"{st['cmd_hz']:.1f}", cmd_color)

        # ramp progress: actual vs setpoint
        target = st["setpoint_hz"]
        frac = min(st["actual_hz"] / target, 1.0) if target > 0 else 0.0
        self.ramp_bar.setValue(int(frac * 1000))
        self.ramp_lbl.setText(
            f"cmd {st['cmd_hz']:5.1f} Hz → set {target:5.1f} Hz · "
            f"actual {st['actual_hz']:5.1f} Hz"
            + ("  (ramping)" if st["ramping"] else ""))

        if st["stale"]:
            self.stale_lbl.setText(f"STALE {st['age_s']:.0f}s — "
                                   f"fan NOT auto-stopped")
            self.stale_lbl.setStyleSheet(f"color: {theme.ERROR}; "
                                         f"font-weight: bold;")
        else:
            self.stale_lbl.setText(f"data {st['age_s']:.1f}s")
            self.stale_lbl.setStyleSheet(f"color: {theme.TEXT_DIM};")

        window = self.config.plot_window_s
        n = int(window / max(self.config.poll_s, 0.02) * 1.1) + 2
        data = self.device.ring.tail(n)
        t = data["t"]
        if t.size >= 2 and self.hz_plot.isVisible():
            x = t - t[-1]
            keep = x >= -window
            for name, curve in self._hz_curves.items():
                xd, yd = _envelope(x[keep], data[name][keep])
                curve.setData(xd, yd)
            factor = calibration.fps_to_unit(1.0, unit)
            xd, yd = _envelope(x[keep],
                               data["velocity_fps"][keep] * factor)
            self._vel_curve.setData(xd, yd)
            self.hz_plot.getPlotItem().setXRange(-window, 0.0, padding=0)
        _ = label

    def apply_settings(self):
        self.hz_spin.setRange(0.0, self.config.max_hz)
        self._hz_spin_changed(self.hz_spin.value())

    def shutdown(self):
        try:
            self.device.disconnect()
        except Exception as exc:                       # noqa: BLE001
            log.warning("shutdown: %s", exc)


class LswtMainWindow(QMainWindow):
    def __init__(self, cfg: Optional[LswtConfig] = None):
        super().__init__()
        self.panel = LswtPanel(cfg, self)
        self.setWindowTitle(f"{self.panel.config.label} — Fan Control")
        self.resize(1080, 780)
        self.setStyleSheet(theme.get_stylesheet())
        self.setCentralWidget(self.panel)
        self.panel.tunnelChanged.connect(
            lambda label: self.setWindowTitle(f"{label} — Fan Control"))

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
        a_defaults.triggered.connect(self.panel.save_defaults)
        m.addAction(a_defaults)
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
        path, _ = QFileDialog.getSaveFileName(
            self, "Save config",
            f"lswt_{self.panel.config.tunnel}_config.json",
            "JSON (*.json)")
        if path:
            self.panel.config.save(path)

    def _load_cfg(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load config", "",
                                              "JSON (*.json)")
        if not path:
            return
        was_connected = self.panel.device.connected
        self.panel._disarm()
        if was_connected:
            self.panel.device.disconnect()
        self.panel.config = LswtConfig.load(path)
        self.panel.device = LswtDrive(self.panel.config)
        self.panel.device.on_status = self.panel.statusSignal.emit
        self.panel.ip_edit.setText(self.panel.config.ip)
        self.panel.sim.setChecked(self.panel.config.force_sim)
        self.panel.tunnel_combo.setCurrentIndex(
            0 if self.panel.config.tunnel == "north" else 1)
        self.panel.apply_settings()
        self.panel._set_connected_ui(False)
        self.setWindowTitle(f"{self.panel.config.label} — Fan Control")
        self.statusBar().showMessage(f"Loaded {path} — reconnect", 3000)

    def closeEvent(self, event):
        self.panel.shutdown()
        super().closeEvent(event)
