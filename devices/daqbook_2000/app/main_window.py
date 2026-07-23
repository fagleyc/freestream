"""Main window + coordinator panel for the DaqBook app.

``DaqbookPanel`` owns the driver and marshals IO-thread callbacks onto the
GUI thread (status via Qt signal; data is pulled from the device ring buffer
by a 10 Hz timer — the ring is the single source of truth, so nothing is
lost between ticks).

``DaqbookMainWindow`` is the standalone shell (status bar + menus); the
panel embeds directly into AeroVIS later.
"""

from __future__ import annotations

import logging
import time
import webbrowser
from pathlib import Path
from typing import Optional

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit,
    QMainWindow, QPushButton, QStatusBar, QTableWidget, QTableWidgetItem,
    QTabWidget, QVBoxLayout, QWidget,
)

from daqbook_2000 import about, theme
from daqbook_2000.config import DaqbookConfig
from daqbook_2000.device import Daqbook2000

from .channels_panel import ChannelsPanel
from .plots import ChannelHistory, ChannelTiles
from .settings_dialog import SettingsDialog

log = logging.getLogger(__name__)

_WINDOWS = [("10 s", 10.0), ("30 s", 30.0), ("2 min", 120.0), ("5 min", 300.0)]

_ABOUT_SUMMARY = (
    "Standalone driver and GUI for the IOtech DaqBook/2005 that digitizes "
    "the USAFA subsonic tunnel's dynamic pressure (Pdiff), total pressure "
    "(Ptot) and temperature (Temp) transducer voltages via the vendor DaqX "
    "API over Ethernet, with engineering-unit scaling seeded from the rig's "
    "PCF transducer slopes. Serves tunnel q to other consumers through "
    "DaqbookAuxSource. Part of the AeroVIS instrument suite.")


def _about_dialog(parent=None) -> QDialog:
    """Small dark-themed About dialog: name, version, contact, history."""
    dlg = QDialog(parent)
    dlg.setWindowTitle(f"About {about.APP_NAME}")
    dlg.setMinimumWidth(560)
    if parent is None:
        dlg.setStyleSheet(theme.get_stylesheet())
    root = QVBoxLayout(dlg)
    root.setSpacing(8)

    name = QLabel(about.APP_NAME)
    name.setWordWrap(True)
    name.setStyleSheet(
        f"font-size: 15pt; font-weight: 600; color: {theme.TEXT};")
    root.addWidget(name)
    ver = QLabel(f"Version {about.__version__}")
    ver.setProperty("mono", "true")
    ver.setStyleSheet(f"font-size: 11pt; font-weight: bold; "
                      f"color: {theme.ACCENT_LIGHT};")
    root.addWidget(ver)

    summary = QLabel(_ABOUT_SUMMARY)
    summary.setWordWrap(True)
    root.addWidget(summary)

    contact = QLabel(
        f'Author: {about.AUTHOR} — '
        f'<a href="mailto:{about.CONTACT}" '
        f'style="color: {theme.ACCENT_LIGHT};">{about.CONTACT}</a>')
    contact.setOpenExternalLinks(True)
    contact.setStyleSheet(f"color: {theme.TEXT_DIM};")
    root.addWidget(contact)

    table = QTableWidget(len(about.VERSION_HISTORY), 3)
    table.setHorizontalHeaderLabels(["Version", "Date", "Changes"])
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setWordWrap(True)
    hdr = table.horizontalHeader()
    hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
    hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
    hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
    for r, (v, d, s) in enumerate(about.VERSION_HISTORY):
        for c, text in enumerate((v, d, s)):
            item = QTableWidgetItem(text)
            if c == 2:
                item.setToolTip(text)
            table.setItem(r, c, item)
    table.resizeRowsToContents()
    table.setMinimumHeight(140)
    root.addWidget(table, 1)

    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    buttons.rejected.connect(dlg.reject)
    root.addWidget(buttons)
    return dlg


class DaqbookPanel(QWidget):
    """The complete DaqBook GUI (also embeddable in host suites).

    ``device``/``embedded`` support hosting the EXACT same panel inside
    Freestream: pass the host's live :class:`Daqbook2000` so only ONE
    driver/acquisition ever exists, and ``embedded=True`` to hide the
    Connection row (the host owns connect/disconnect AND the scan rate —
    the suite-wide sample rate replaces the panel's rate spin). With the
    defaults the standalone app behaviour is unchanged — the panel builds
    and owns its own device.
    """

    statusSignal = pyqtSignal(str)

    def __init__(self, cfg: Optional[DaqbookConfig] = None, parent=None,
                 *, device: Optional[Daqbook2000] = None,
                 embedded: bool = False):
        super().__init__(parent)
        self.setObjectName("root")
        self._embedded = bool(embedded)
        if device is not None:
            self.device = device
            self.config = cfg if cfg is not None else device.config
        else:
            self.config = cfg or DaqbookConfig()
            self.device = Daqbook2000(self.config)

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
        self._ui_timer.setInterval(100)               # 10 Hz redraw
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

    # ── UI ──
    def _build_ui(self):
        self.setStyleSheet(theme.get_stylesheet())
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # connection bar
        conn = self.conn_group = QGroupBox("Connection")
        cl = QHBoxLayout(conn)
        cl.setSpacing(8)
        cl.addWidget(QLabel("Device"))
        self.name_edit = QLineEdit(self.config.device_name)
        self.name_edit.setFixedWidth(130)
        self.name_edit.setToolTip(
            "Alias from the Daq Configuration applet (DaqX64.cpl / "
            "DaqXCPL.exe),\nwhich maps it to the device IP "
            f"({self.config.device_ip}).")
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
        self.lamp.setStyleSheet(f"color: {theme.TEXT_DIM}; font-weight: bold;")
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

        # tabs
        self.tabs = QTabWidget()

        live = QWidget()
        ll = QVBoxLayout(live)
        ll.setSpacing(8)
        self.tiles = ChannelTiles()
        self.tiles.avg_ms = self.config.tile_avg_ms
        ll.addWidget(self.tiles)

        hist_bar = QHBoxLayout()
        hist_bar.addWidget(QLabel("Window"))
        self.window_combo = QComboBox()
        for label, _s in _WINDOWS:
            self.window_combo.addItem(label)
        self.window_combo.setCurrentIndex(1)
        self.window_combo.currentIndexChanged.connect(self._window_changed)
        hist_bar.addWidget(self.window_combo)
        self.volts_check = QCheckBox("Show raw volts")
        self.volts_check.toggled.connect(self._toggle_volts)
        hist_bar.addWidget(self.volts_check)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._toggle_pause)
        hist_bar.addWidget(self.pause_btn)
        hist_bar.addStretch(1)
        ll.addLayout(hist_bar)

        self.history = ChannelHistory()
        self.history.window_s = self.config.plot_window_s
        ll.addWidget(self.history, 1)
        self.tabs.addTab(live, "Live")

        self.channels_panel = ChannelsPanel(self.config)
        self.tabs.addTab(self.channels_panel, "Channels")
        root.addWidget(self.tabs, 1)

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
        self.history.set_show_volts(self.volts_check.isChecked())
        self._last_count = 0
        self._last_time = time.perf_counter()

    def _handle_disconnect(self):
        self.device.disconnect()
        self._set_connected_ui(False)

    def _set_connected_ui(self, connected: bool):
        self.connect_btn.setEnabled(not connected)
        self.disconnect_btn.setEnabled(connected)
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

    def _toggle_volts(self, show: bool):
        self.history.set_show_volts(show)

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

    def _slow_tick(self):
        now = time.perf_counter()
        if self._last_time and now > self._last_time:
            dn = self.device.frame_count() - self._last_count
            self._rate = dn / (now - self._last_time)
        self._last_time = now
        self._last_count = self.device.frame_count()
        self.history.note_rate(self.device.actual_hz or self._rate)

    # ── teardown ──
    def shutdown(self):
        try:
            self.device.disconnect()
        except Exception as exc:                       # noqa: BLE001
            log.warning("shutdown: %s", exc)

    def apply_settings(self):
        """Re-read display settings from config (after Settings dialog)."""
        self.tiles.avg_ms = self.config.tile_avg_ms
        self.history.window_s = self.config.plot_window_s
        self.rate_spin.setValue(self.config.scan_hz)
        self.name_edit.setText(self.config.device_name)


class DaqbookMainWindow(QMainWindow):
    def __init__(self, cfg: Optional[DaqbookConfig] = None):
        super().__init__()
        self.setWindowTitle("DaqBook/2000 — Tunnel Conditions DAQ")
        self.resize(1100, 780)
        self.setStyleSheet(theme.get_stylesheet())
        self.panel = DaqbookPanel(cfg, self)
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
        self._rate_timer.timeout.connect(self._update_rate)
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

        h = self.menuBar().addMenu("&Help")
        a_docs = QAction("&Documentation", self)
        a_docs.triggered.connect(self._open_docs)
        h.addAction(a_docs)
        h.addSeparator()
        a_about = QAction(f"&About {about.APP_NAME.split(' — ')[0]}", self)
        a_about.triggered.connect(self._show_about)
        h.addAction(a_about)

    def _open_docs(self):
        docs = Path(__file__).resolve().parents[1] / "docs" / "index.html"
        webbrowser.open(docs.resolve().as_uri())

    def _show_about(self):
        _about_dialog(self).exec()

    def _open_settings(self):
        dlg = SettingsDialog(self.panel.config, self)
        if dlg.exec():
            self.panel.apply_settings()
            self.statusBar().showMessage("Settings applied", 3000)

    def _save_cfg(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save config",
                                              "daqbook_config.json",
                                              "JSON (*.json)")
        if path:
            self.panel.config.save(path)

    def _load_cfg(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load config", "",
                                              "JSON (*.json)")
        if path:
            self.panel.config = DaqbookConfig.load(path)
            self.panel.device.config = self.panel.config
            self.panel.channels_panel._cfg = self.panel.config
            self.panel.channels_panel.reload()
            self.panel.apply_settings()
            self.statusBar().showMessage(f"Loaded {path}", 3000)

    def _update_rate(self):
        self._sb_rate.setText(f"{self.panel._rate:7.1f} scans/s")

    def closeEvent(self, event):
        self.panel.shutdown()
        super().closeEvent(event)
