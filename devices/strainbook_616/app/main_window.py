"""Main window + coordinator panel for the StrainBook app.

Same structure as the DaqBook app: the driver owns the ring buffer, the
GUI pulls from it on a 10 Hz timer, status is marshalled via a Qt signal.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QPushButton, QStatusBar,
    QTabWidget, QVBoxLayout, QWidget,
)

from strainbook_616 import theme
from strainbook_616.config import StrainbookConfig
from strainbook_616.device import Strainbook616

from .channels_panel import ChannelsPanel
from .forces_panel import ForcesPanel
from .plots import BridgeHistory, ChannelTiles
from .settings_dialog import SettingsDialog

log = logging.getLogger(__name__)

_WINDOWS = [("10 s", 10.0), ("30 s", 30.0), ("2 min", 120.0),
            ("5 min", 300.0)]


class StrainbookPanel(QWidget):
    """The complete StrainBook GUI (also embeddable in host suites).

    ``device``/``embedded`` support hosting the EXACT same panel inside
    Freestream: pass the host's live :class:`Strainbook616` so only ONE
    driver/acquisition ever exists, and ``embedded=True`` to hide the
    Connection row (the host owns connect/disconnect AND the scan rate —
    the suite-wide sample rate replaces the panel's rate spin). With the
    defaults the standalone app behaviour is unchanged — the panel builds
    and owns its own device.
    """

    statusSignal = pyqtSignal(str)

    def __init__(self, cfg: Optional[StrainbookConfig] = None, parent=None,
                 *, device: Optional[Strainbook616] = None,
                 embedded: bool = False):
        super().__init__(parent)
        self.setObjectName("root")
        self._embedded = bool(embedded)
        if device is not None:
            self.device = device
            self.config = cfg if cfg is not None else device.config
        else:
            self.config = cfg or StrainbookConfig()
            self.device = Strainbook616(self.config)

        self._rate = 0.0
        self._last_count = 0
        self._last_time = 0.0

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

        self._slow_timer = QTimer(self)
        self._slow_timer.setInterval(500)
        self._slow_timer.timeout.connect(self._slow_tick)
        self._slow_timer.start()

        self._last_connected = self.device.connected
        if self._last_connected:                # embedded, already-live host
            self._attach_channels()
        self._set_connected_ui(self._last_connected)

    def _build_ui(self):
        self.setStyleSheet(theme.get_stylesheet())
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        conn = self.conn_group = QGroupBox("Connection")
        cl = QHBoxLayout(conn)
        cl.setSpacing(8)
        cl.addWidget(QLabel("Device"))
        self.name_edit = QLineEdit(self.config.device_name)
        self.name_edit.setFixedWidth(130)
        self.name_edit.setToolTip(
            f"DaqX alias (applet); maps to {self.config.device_ip}")
        cl.addWidget(self.name_edit)
        cl.addWidget(QLabel("Rate"))
        self.rate_spin = QDoubleSpinBox()
        self.rate_spin.setRange(1.0, 100_000.0)
        self.rate_spin.setDecimals(0)
        self.rate_spin.setValue(self.config.scan_hz)
        self.rate_spin.setSuffix(" Hz")
        cl.addWidget(self.rate_spin)
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
        if self._embedded:
            # host owns connect/disconnect AND the scan rate (the suite's
            # Measurement Setup is the single sample-rate editor)
            conn.hide()

        self.tabs = QTabWidget()

        live = QWidget()
        ll = QVBoxLayout(live)
        ll.setSpacing(8)
        self.tiles = ChannelTiles()
        self.tiles.avg_ms = self.config.tile_avg_ms
        ll.addWidget(self.tiles)

        bar = QHBoxLayout()
        self.tare_btn = QPushButton("Tare (zero bridges)")
        self.tare_btn.setObjectName("success")
        self.tare_btn.clicked.connect(lambda: self.device.tare())
        bar.addWidget(self.tare_btn)
        self.clear_tare_btn = QPushButton("Clear tare")
        self.clear_tare_btn.clicked.connect(self.device.clear_tare)
        bar.addWidget(self.clear_tare_btn)
        bar.addSpacing(24)
        bar.addWidget(QLabel("Window"))
        self.window_combo = QComboBox()
        for label, _s in _WINDOWS:
            self.window_combo.addItem(label)
        self.window_combo.setCurrentIndex(1)
        self.window_combo.currentIndexChanged.connect(self._window_changed)
        bar.addWidget(self.window_combo)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._toggle_pause)
        bar.addWidget(self.pause_btn)
        self.follow_btn = QPushButton("Follow live")
        self.follow_btn.setToolTip("Re-pin the time axis to 'now' after "
                                   "zooming/panning the plot")
        self.follow_btn.clicked.connect(
            lambda: self.history.set_follow(True))
        bar.addWidget(self.follow_btn)
        bar.addStretch(1)
        ll.addLayout(bar)

        # per-channel plot visibility toggles (rebuilt on channel attach)
        self.chan_row = QHBoxLayout()
        self.chan_row.setSpacing(10)
        self._chan_checks = {}
        ll.addLayout(self.chan_row)

        self.history = BridgeHistory()
        self.history.window_s = self.config.plot_window_s
        ll.addWidget(self.history, 1)
        self.tabs.addTab(live, "Live")

        self.forces_panel = ForcesPanel(self.config)
        self.forces_panel.balanceConfigChanged.connect(
            self._on_balance_config_changed)
        self.tabs.addTab(self.forces_panel, "Forces")

        self.channels_panel = ChannelsPanel(self.config)
        self.tabs.addTab(self.channels_panel, "Channels")
        root.addWidget(self.tabs, 1)

        # auto-load a previously used .vol calibration
        if self.config.vol_path:
            self.forces_panel.load_vol(self.config.vol_path)

    # ── balance layout (Force ↔ Moment) ──
    def _on_balance_config_changed(self, text: str):
        """Route a Forces-tab layout pick to the driver: it renames the four
        bridge channels on the live device (config + ring/tare), then the
        Channels table and the live tiles/history rebind to the new names."""
        self.device.set_balance_config(text)
        if self.device.connected:
            self._attach_channels()
        self.channels_panel.reload()

    # ── connect / disconnect ──
    def _handle_connect(self):
        self.config.device_name = self.name_edit.text().strip() or \
            self.config.device_name
        self.config.scan_hz = float(self.rate_spin.value())
        self.config.force_sim = self.sim.isChecked()
        try:
            self.device.connect()
            self.device.start()
        except Exception as exc:                       # noqa: BLE001
            self.statusSignal.emit(f"Connect failed: {exc}")
            log.exception("connect failed")
            self.device.disconnect()
            return
        self._attach_channels()
        self._set_connected_ui(True)

    def _attach_channels(self):
        """Bind tiles/history to the (re)started acquisition's channels."""
        chans = self.config.enabled_channels()
        self.tiles.set_channels(chans)
        self.history.set_channels(chans, self.device.ring)
        self._rebuild_channel_toggles(chans)
        self._last_count = 0
        self._last_time = time.perf_counter()

    def _rebuild_channel_toggles(self, chans):
        """One colored checkbox per channel — toggles the curve on the
        history plot (the excitation strip collapses when its channel is
        hidden). Visibility state lives in the history widget, keyed by
        name, so it survives rebinds."""
        while self.chan_row.count():
            item = self.chan_row.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._chan_checks = {}
        lbl = QLabel("Show")
        lbl.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.chan_row.addWidget(lbl)
        for i, ch in enumerate(chans):
            chk = QCheckBox(ch.name)
            chk.setChecked(self.history.channel_visible(ch.name))
            chk.setStyleSheet(
                f"QCheckBox {{ color: {theme.series_color(i)}; "
                f"font-weight: bold; }}")
            chk.setToolTip(f"Show/hide {ch.name} on the plot")
            chk.toggled.connect(
                lambda on, n=ch.name:
                self.history.set_channel_visible(n, on))
            self._chan_checks[ch.name] = chk
            self.chan_row.addWidget(chk)
        self.chan_row.addStretch(1)

    def _handle_disconnect(self):
        self.device.disconnect()
        self._set_connected_ui(False)

    def _set_connected_ui(self, connected: bool):
        self.connect_btn.setEnabled(not connected)
        self.disconnect_btn.setEnabled(connected)
        self.tare_btn.setEnabled(connected)
        self.clear_tare_btn.setEnabled(connected)
        for w in (self.name_edit, self.rate_spin, self.sim):
            w.setEnabled(not connected)
        if not connected:
            self._set_lamp("DISCONNECTED", theme.TEXT_DIM)
        elif self.device.sim_mode:
            self._set_lamp("SIMULATION", theme.WARNING)
        else:
            self._set_lamp(f"ACQUIRING @ {self.device.actual_hz:.0f} Hz",
                           theme.SUCCESS)

    def _set_lamp(self, text: str, color: str):
        self.lamp.setText(text)
        self.lamp.setStyleSheet(f"color: {color}; font-weight: bold;")

    # ── live controls ──
    def _window_changed(self, idx: int):
        self.history.window_s = _WINDOWS[idx][1]

    def _toggle_pause(self, paused: bool):
        self.history.paused = paused
        self.pause_btn.setText("Resume" if paused else "Pause")

    # ── timers ──
    def _refresh_ui(self):
        # track connection changes made OUTSIDE the panel's own buttons
        # (embedded hosts) so tiles/history bind and the controls come
        # alive / lock down without a Connect click.
        connected = self.device.connected
        if connected != self._last_connected:
            self._last_connected = connected
            if connected:
                self._attach_channels()
            self._set_connected_ui(connected)
        if not connected:
            return
        self.tiles.refresh(self.device.ring, self.device.actual_hz)
        self.history.refresh()
        self.forces_panel.refresh(self.device.ring, self.device.actual_hz,
                                  self.history.window_s,
                                  zero_count=getattr(self.device,
                                                     "tare_count", None))
        if self.forces_panel.overstress:
            self.statusSignal.emit("⚠ BALANCE OVERSTRESS — reduce load!")

    def _slow_tick(self):
        now = time.perf_counter()
        if self._last_time and now > self._last_time:
            dn = self.device.frame_count() - self._last_count
            self._rate = dn / (now - self._last_time)
        self._last_time = now
        self._last_count = self.device.frame_count()
        self.history.note_rate(self.device.actual_hz or self._rate)

    def shutdown(self):
        try:
            self.device.disconnect()
        except Exception as exc:                       # noqa: BLE001
            log.warning("shutdown: %s", exc)

    def apply_settings(self):
        self.tiles.avg_ms = self.config.tile_avg_ms
        self.history.window_s = self.config.plot_window_s
        self.rate_spin.setValue(self.config.scan_hz)
        self.name_edit.setText(self.config.device_name)


class StrainbookMainWindow(QMainWindow):
    def __init__(self, cfg: Optional[StrainbookConfig] = None):
        super().__init__()
        self.setWindowTitle("StrainBook/616 — Balance Bridge DAQ")
        self.resize(1200, 800)
        self.setStyleSheet(theme.get_stylesheet())
        self.panel = StrainbookPanel(cfg, self)
        self.setCentralWidget(self.panel)

        sb = QStatusBar()
        self.setStatusBar(sb)
        self._sb_status = QLabel("Idle")
        self._sb_status.setProperty("mono", "true")
        sb.addPermanentWidget(self._sb_status, 1)
        self._sb_rate = QLabel("— scans/s")
        self._sb_rate.setProperty("mono", "true")
        sb.addPermanentWidget(self._sb_rate)
        self.panel.statusSignal.connect(self._sb_status.setText)

        self._rate_timer = QTimer(self)
        self._rate_timer.setInterval(500)
        self._rate_timer.timeout.connect(
            lambda: self._sb_rate.setText(f"{self.panel._rate:7.1f} scans/s"))
        self._rate_timer.start()

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
                                              "strainbook_config.json",
                                              "JSON (*.json)")
        if path:
            self.panel.config.save(path)

    def _load_cfg(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load config", "",
                                              "JSON (*.json)")
        if path:
            self.panel.config = StrainbookConfig.load(path)
            self.panel.device.config = self.panel.config
            self.panel.channels_panel._cfg = self.panel.config
            self.panel.channels_panel.reload()
            self.panel.apply_settings()
            self.statusBar().showMessage(f"Loaded {path}", 3000)

    def closeEvent(self, event):
        self.panel.shutdown()
        super().closeEvent(event)
