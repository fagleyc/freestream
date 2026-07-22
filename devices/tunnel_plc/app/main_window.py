"""Main window for the SSWT tunnel app.

Layout: connection bar → RPM readouts + status-light grid → RPM history
plot → control section. The control section is visibly DISARMED until
the operator arms writes (which also requires ``rpm_max`` to be
configured); arming constructs the TunnelControl — before that, no
write-capable object even exists in the process.
"""

from __future__ import annotations

import logging
import time
from typing import Dict, Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QStatusBar, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from tunnel_plc import theme
from tunnel_plc.config import TunnelConfig
from tunnel_plc.control import TunnelControl, WriteRefused
from tunnel_plc.gateway import GatewayError
from tunnel_plc.monitor import TunnelMonitor
from tunnel_plc.registers import TunnelSnapshot

from .settings_dialog import SettingsDialog
from .widgets import FanRotor, LedLamp, RpmGauge

log = logging.getLogger(__name__)

theme.apply_pyqtgraph_theme()

_MAX_PLOT_BINS = 1200

# (snapshot attribute, label, True if lit == BAD)
_LIGHTS = [
    ("fan_running",             "Fan running",        False),
    ("console_control",         "Console control",    False),
    ("inverter_fault",          "INVERTER FAULT",     True),
    ("oil_level_low",           "OIL LEVEL LOW",      True),
    ("bearing_temp_low",        "Bearing temp low",   True),
    ("bearing_heater_on",       "Bearing heater",     False),
    ("tunnel_fan_light_start",  "Tunnel fan start",   False),
    ("tunnel_fan_light_stop",   "Tunnel fan stop",    False),
    ("cooling_fan_light_start", "Cooling fan start",  False),
    ("cooling_fan_light_stop",  "Cooling fan stop",   False),
]


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


class TunnelPanel(QWidget):
    """The complete tunnel GUI (also embeddable in host suites).

    ``monitor``/``embedded`` support hosting the EXACT same panel inside
    Freestream: pass the host's live :class:`TunnelMonitor` so only ONE
    monitor/gateway connection ever exists, and ``embedded=True`` to hide
    the Connection row (the host owns connect/disconnect). With the
    defaults the standalone app behaviour is unchanged — the panel builds
    and owns its own monitor.

    The arming safety is IDENTICAL either way: no write-capable
    TunnelControl object exists until the operator arms writes here, and
    arming still requires ``rpm_max > 0``. An armed panel creates its own
    TunnelControl against the SHARED monitor — writes stay guarded by
    the driver's rpm_max clamp and momentary-button protocol.
    """

    statusSignal = pyqtSignal(str)

    def __init__(self, cfg: Optional[TunnelConfig] = None, parent=None,
                 *, monitor: Optional[TunnelMonitor] = None,
                 embedded: bool = False):
        super().__init__(parent)
        self.setObjectName("root")
        self._embedded = bool(embedded)
        if monitor is not None:
            self.monitor = monitor
            self.config = cfg if cfg is not None else monitor.config
        else:
            self.config = cfg or TunnelConfig()
            self.monitor = TunnelMonitor(self.config)
        self.control: Optional[TunnelControl] = None    # exists only armed

        self._build_ui()
        if not self._embedded:
            # standalone: pipe driver status to the main-window status bar.
            # Embedded hosts keep their own on_status wiring — grabbing it
            # here would leave a dangling callback into a deleted panel
            # after the host closes the containing dialog.
            self.monitor.on_status = self.statusSignal.emit

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(200)
        self._ui_timer.timeout.connect(self._refresh_ui)
        self._ui_timer.start()

        self._last_connected = self.monitor.running
        self._set_connected_ui(self._last_connected)

    # ── UI ──
    def _build_ui(self):
        self.setStyleSheet(theme.get_stylesheet())
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        conn = self.conn_group = \
            QGroupBox("Connection — Red Lion G315 Modbus gateway")
        cl = QHBoxLayout(conn)
        cl.setSpacing(8)
        cl.addWidget(QLabel("Gateway IP"))
        self.ip_edit = QLineEdit(self.config.ip)
        self.ip_edit.setFixedWidth(120)
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
        root.addWidget(conn)
        if self._embedded:                  # host owns connect/disconnect
            conn.hide()

        mid = QHBoxLayout()

        rpm_box = QGroupBox("Fan speed")
        rg = QGridLayout(rpm_box)
        rg.setContentsMargins(6, 4, 6, 4)
        self.rotor = FanRotor()
        rg.addWidget(self.rotor, 0, 0,
                     alignment=pg.QtCore.Qt.AlignmentFlag.AlignLeft |
                     pg.QtCore.Qt.AlignmentFlag.AlignTop)
        self.gauge = RpmGauge()
        self.gauge.setMaximumHeight(340)
        rg.addWidget(self.gauge, 0, 0, 2, 2)
        rg.setRowStretch(1, 1)
        rg.setColumnStretch(1, 1)
        rpm_box.setMaximumHeight(390)
        mid.addWidget(rpm_box, 3)

        lights_box = QGroupBox("Status lights (VersaMax)")
        lg = QGridLayout(lights_box)
        lg.setContentsMargins(10, 6, 10, 6)
        lg.setVerticalSpacing(2)
        self.lamps: Dict[str, LedLamp] = {}
        for i, (attr, label, bad) in enumerate(_LIGHTS):
            lamp = LedLamp(label, bad)
            self.lamps[attr] = lamp
            lg.addWidget(lamp, i % 5, i // 5)
        # bearing temperature readouts (— until config.bearing_temps is
        # enabled AND the Crimson block is extended — see README)
        self.bearing_vals: Dict[str, QLabel] = {}
        for i, attr in enumerate(("bearing_b1", "bearing_b2",
                                  "bearing_b3")):
            name = QLabel(f"Bearing B{i + 1}")
            name.setStyleSheet(f"color: {theme.TEXT_DIM};")
            lg.addWidget(name, 5 + i, 0)
            val = QLabel("—")
            val.setStyleSheet("font-family: Consolas, monospace; "
                              f"color: {theme.TEXT};")
            val.setToolTip("Enable bearing temps in File → Settings "
                           "(needs the extended Crimson gateway block)")
            self.bearing_vals[attr] = val
            lg.addWidget(val, 5 + i, 1)
        lights_box.setMaximumHeight(390)
        mid.addWidget(lights_box, 2)
        root.addLayout(mid, 3)

        # RPM history
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
        pi.setLabel("left", "fan speed  (RPM)")
        pi.setLabel("bottom", "time before now  (s)")
        pi.addLegend(offset=(8, 8), labelTextColor=theme.TEXT,
                     brush=pg.mkBrush(theme.PLOT_BG + "cc"),
                     pen=pg.mkPen(theme.BORDER))
        self._curves = {
            "actual_rpm": pi.plot([], [], name="Actual RPM",
                                  pen=pg.mkPen(theme.series_color(0),
                                               width=2)),
            "rpm_set": pi.plot([], [], name="RPM set",
                               pen=pg.mkPen(theme.series_color(2), width=1,
                                            style=pg.QtCore.Qt.PenStyle
                                            .DashLine)),
        }
        self.plot.setMinimumHeight(150)
        root.addWidget(self.plot, 2)

        root.addWidget(self._build_control_box())

    def _build_control_box(self) -> QGroupBox:
        self.ctrl_box = QGroupBox("Control — DISARMED")
        cg = QGridLayout(self.ctrl_box)

        self.arm_btn = QPushButton("ARM WRITES")
        self.arm_btn.setCheckable(True)
        self.arm_btn.setObjectName("danger")
        self.arm_btn.setMinimumHeight(40)
        self.arm_btn.setToolTip(
            "Writes stay impossible until armed (no TunnelControl object "
            "exists). Requires rpm_max to be configured in Settings.")
        self.arm_btn.clicked.connect(self._handle_arm)
        cg.addWidget(self.arm_btn, 0, 0, 2, 1)

        cg.addWidget(QLabel("RPM setpoint"), 0, 1)
        self.rpm_spin = QDoubleSpinBox()
        self.rpm_spin.setRange(0, 0)          # widened when armed
        self.rpm_spin.setDecimals(0)
        cg.addWidget(self.rpm_spin, 0, 2)
        self.rpm_apply = QPushButton("Apply RPM")
        self.rpm_apply.setObjectName("primary")
        self.rpm_apply.clicked.connect(self._apply_rpm)
        cg.addWidget(self.rpm_apply, 0, 3)

        self.fan_start = QPushButton("Tunnel fan START")
        self.fan_start.clicked.connect(
            lambda: self._command("start_tunnel_fan"))
        cg.addWidget(self.fan_start, 1, 1)
        self.fan_stop = QPushButton("Tunnel fan STOP")
        self.fan_stop.clicked.connect(
            lambda: self._command("stop_tunnel_fan"))
        cg.addWidget(self.fan_stop, 1, 2)
        self.cool_start = QPushButton("Cooling fan START")
        self.cool_start.clicked.connect(
            lambda: self._command("start_cooling_fan"))
        cg.addWidget(self.cool_start, 1, 3)
        self.cool_stop = QPushButton("Cooling fan STOP")
        self.cool_stop.clicked.connect(
            lambda: self._command("stop_cooling_fan"))
        cg.addWidget(self.cool_stop, 1, 4)

        self.write_table = QTableWidget(0, 4)
        self.write_table.setHorizontalHeaderLabels(
            ["Time", "Tag", "Old → New", "Note"])
        self.write_table.horizontalHeader().setStretchLastSection(True)
        self.write_table.verticalHeader().setVisible(False)
        self.write_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.write_table.setColumnWidth(0, 90)
        self.write_table.setColumnWidth(1, 190)
        self.write_table.setColumnWidth(2, 160)
        self.write_table.setMaximumHeight(140)
        cg.addWidget(self.write_table, 2, 0, 1, 5)
        self._logged_writes = 0
        return self.ctrl_box

    # ── actions ──
    def _handle_connect(self):
        self.config.ip = self.ip_edit.text().strip() or self.config.ip
        if self.sim.isChecked() != self.config.force_sim or \
                self.config.ip != self.monitor.config.ip:
            self.config.force_sim = self.sim.isChecked()
            self.monitor = TunnelMonitor(self.config)   # rebuild transport
            self.monitor.on_status = self.statusSignal.emit
        try:
            self.monitor.connect()
        except Exception as exc:                       # noqa: BLE001
            self.statusSignal.emit(f"Connect failed: {exc}")
            log.exception("connect failed")
            self.monitor.disconnect()
            return
        self._set_connected_ui(True)

    def _handle_disconnect(self):
        self._disarm()
        self.monitor.disconnect()
        self._set_connected_ui(False)

    def _handle_arm(self):
        if not self.arm_btn.isChecked():
            self._disarm()
            return
        if self.config.rpm_max <= 0:
            self.arm_btn.setChecked(False)
            QMessageBox.warning(
                self, "Cannot arm",
                "rpm_max is not configured (0). Set the real fan speed "
                "limit in File → Settings before arming writes.")
            return
        if not self.monitor.running:
            self.arm_btn.setChecked(False)
            return
        confirm = QMessageBox.question(
            self, "Arm tunnel writes?",
            f"This enables commands to REAL MACHINERY:\n"
            f"fan start/stop and RPM setpoint (max {self.config.rpm_max:g}"
            f" RPM).\n\nArm writes?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirm != QMessageBox.StandardButton.Yes:
            self.arm_btn.setChecked(False)
            return
        self.control = TunnelControl(self.config, self.monitor,
                                     enable_writes=True)
        self.control.on_status = self.statusSignal.emit
        self.rpm_spin.setRange(0, self.config.rpm_max)
        self._set_armed_ui(True)
        self.statusSignal.emit("Writes ARMED")

    def _disarm(self):
        self.control = None
        self.arm_btn.setChecked(False)
        self._set_armed_ui(False)

    def _apply_rpm(self):
        if self.control is None:
            return
        try:
            self.control.set_rpm(self.rpm_spin.value())
        except (WriteRefused, GatewayError) as exc:
            self.statusSignal.emit(str(exc))
            log.error("set_rpm failed: %s", exc)

    def _command(self, method: str):
        if self.control is None:
            return
        try:
            getattr(self.control, method)()
        except (WriteRefused, GatewayError) as exc:
            self.statusSignal.emit(str(exc))
            log.error("%s failed: %s", method, exc)

    # ── UI state ──
    def _set_connected_ui(self, connected: bool):
        self.connect_btn.setEnabled(not connected)
        self.disconnect_btn.setEnabled(connected)
        for w in (self.ip_edit, self.sim):
            w.setEnabled(not connected)
        self.arm_btn.setEnabled(connected)
        if not connected:
            self._disarm()
            self._set_lamp("DISCONNECTED", theme.TEXT_DIM)
            self.stale_lbl.setText("")
        elif self.monitor.sim_mode:
            self._set_lamp("SIMULATION", theme.WARNING)
        else:
            self._set_lamp("LIVE (read-only)", theme.SUCCESS)

    def _set_armed_ui(self, armed: bool):
        self.ctrl_box.setTitle("Control — ARMED" if armed
                               else "Control — DISARMED")
        for w in (self.rpm_spin, self.rpm_apply, self.fan_start,
                  self.fan_stop, self.cool_start, self.cool_stop):
            w.setEnabled(armed)
        if armed:
            self._set_lamp("LIVE + ARMED" if not self.monitor.sim_mode
                           else "SIM + ARMED", theme.ERROR)
        elif self.monitor.running:
            self._set_connected_ui(True)

    def _set_lamp(self, text: str, color: str):
        self.lamp.setText(text)
        self.lamp.setStyleSheet(f"color: {color}; font-weight: bold;")

    # ── refresh ──
    def _refresh_ui(self):
        # track connection changes made OUTSIDE the panel's own buttons
        # (embedded hosts, watchdog) so the controls come alive / lock
        # down — and writes DISARM — without a Connect/Disconnect click.
        running = self.monitor.running
        if running != self._last_connected:
            self._last_connected = running
            self._set_connected_ui(running)
        if not running:
            return
        snap: TunnelSnapshot = self.monitor.snapshot()
        self.gauge.set_state(snap.actual_rpm, snap.rpm_set,
                             self.config.rpm_max)
        self.rotor.set_state(snap.actual_rpm, snap.fan_running)
        for attr, lamp in self.lamps.items():
            lamp.set_lit(bool(getattr(snap, attr)))
        unit = getattr(self.config, "bearing_unit", "°C")
        for attr, val in self.bearing_vals.items():
            v = getattr(snap, attr, None)
            val.setText("—" if v is None else f"{v:.1f} {unit}")
        if snap.stale:
            self.stale_lbl.setText(f"STALE {snap.age_s:.0f}s")
            self.stale_lbl.setStyleSheet(f"color: {theme.ERROR}; "
                                         f"font-weight: bold;")
        else:
            self.stale_lbl.setText(f"data {snap.age_s:.1f}s")
            self.stale_lbl.setStyleSheet(f"color: {theme.TEXT_DIM};")

        if self.control is not None:
            self._sync_write_table()

        window = self.config.plot_window_s
        n = int(window / max(self.config.poll_s, 0.05) * 1.1) + 2
        data = self.monitor.ring.tail(n)
        t = data["t"]
        if t.size >= 2 and self.plot.isVisible():
            x = t - t[-1]
            keep = x >= -window
            for name, curve in self._curves.items():
                xd, yd = _envelope(x[keep], data[name][keep])
                curve.setData(xd, yd)
            self.plot.getPlotItem().setXRange(-window, 0.0, padding=0)

    def _sync_write_table(self):
        recs = list(self.control.write_log)
        for rec in recs[self._logged_writes:]:
            row = self.write_table.rowCount()
            self.write_table.insertRow(row)
            ts = time.strftime("%H:%M:%S", time.localtime(rec.t))
            for col, v in enumerate([ts, rec.tag,
                                     f"{rec.old} → {rec.new}", rec.note]):
                self.write_table.setItem(row, col, QTableWidgetItem(str(v)))
            self.write_table.scrollToBottom()
        self._logged_writes = len(recs)

    def shutdown(self):
        try:
            self._disarm()
            self.monitor.disconnect()
        except Exception as exc:                       # noqa: BLE001
            log.warning("shutdown: %s", exc)


class TunnelMainWindow(QMainWindow):
    def __init__(self, cfg: Optional[TunnelConfig] = None):
        super().__init__()
        self.setWindowTitle("SSWT Tunnel — Red Lion G315")
        self.resize(1000, 740)
        self.setStyleSheet(theme.get_stylesheet())
        self.panel = TunnelPanel(cfg, self)
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
            self.statusBar().showMessage("Settings applied", 3000)

    def _save_cfg(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save config",
                                              "tunnel_config.json",
                                              "JSON (*.json)")
        if path:
            self.panel.config.save(path)

    def _load_cfg(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load config", "",
                                              "JSON (*.json)")
        if not path:
            return
        was_connected = self.panel.monitor.running
        self.panel._disarm()
        if was_connected:
            self.panel.monitor.disconnect()
        self.panel.config = TunnelConfig.load(path)
        self.panel.monitor = TunnelMonitor(self.panel.config)
        self.panel.monitor.on_status = self.panel.statusSignal.emit
        self.panel.ip_edit.setText(self.panel.config.ip)
        self.panel.sim.setChecked(self.panel.config.force_sim)
        self.panel._set_connected_ui(False)
        self.statusBar().showMessage(f"Loaded {path} — reconnect", 3000)

    def closeEvent(self, event):
        self.panel.shutdown()
        super().closeEvent(event)
