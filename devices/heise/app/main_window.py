"""Heise PM indicator GUI — live pressure/temperature readout.

Layout: connection bar (COM port + Search, baud, sim, lamp) → big value
tiles per port with the pressure-unit selector, Zero / damping /
battery controls → History plot (one stacked axis per port — the units
differ, so they never share a y-axis).

``HeisePanel`` is embeddable in host suites (pass ``device=`` with the
host's live :class:`HeiseGauge` and ``embedded=True`` to hide the
connection row); standalone behaviour is unchanged with the defaults.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QMainWindow, QMessageBox, QPushButton,
    QStatusBar, QTabWidget, QVBoxLayout, QWidget,
)

from heise import theme
from heise.config import PRESSURE_UNITS, BAUD_RATES, HeiseConfig
from heise.device import HeiseGauge
from heise.protocol import HeiseError

log = logging.getLogger(__name__)

theme.apply_pyqtgraph_theme()


class _PortTile(QGroupBox):
    """Big-number readout for one indicator port."""

    def __init__(self, name: str, unit: str, color: str, parent=None):
        super().__init__(name, parent)
        self.port_name = name
        v = QGridLayout(self)
        chip = QLabel()
        chip.setFixedSize(10, 10)
        chip.setStyleSheet(f"background-color: {color}; "
                           f"border-radius: 5px;")
        v.addWidget(chip, 0, 0)
        self.value_lbl = QLabel("--")
        self.value_lbl.setStyleSheet(
            "font-family: 'Segoe UI'; font-size: 30pt; font-weight: 600; "
            f"color: {theme.TEXT};")
        v.addWidget(self.value_lbl, 0, 1)
        self.unit_lbl = QLabel(unit)
        self.unit_lbl.setObjectName("dim")
        self.unit_lbl.setStyleSheet(
            f"font-size: 14pt; color: {theme.TEXT_DIM};")
        v.addWidget(self.unit_lbl, 0, 2,
                    alignment=Qt.AlignmentFlag.AlignBottom)
        v.setColumnStretch(1, 1)

    def set_value(self, value: Optional[float]) -> None:
        if value is None or not np.isfinite(value):
            self.value_lbl.setText("--")
        else:
            self.value_lbl.setText(f"{value:+,.4f}")

    def set_unit(self, unit: str) -> None:
        self.unit_lbl.setText(unit)


class HeisePanel(QWidget):
    """The complete Heise indicator GUI (also embeddable)."""

    statusSignal = pyqtSignal(str)
    searchDone = pyqtSignal(list)       # [comscan.ProbeResult]

    def __init__(self, cfg: Optional[HeiseConfig] = None, parent=None,
                 *, device: Optional[HeiseGauge] = None,
                 embedded: bool = False):
        super().__init__(parent)
        self.setObjectName("root")
        self._embedded = bool(embedded)
        if device is not None:
            self.device = device
            self.config = cfg if cfg is not None else device.config
        else:
            self.config = cfg or HeiseConfig()
            self.device = HeiseGauge(self.config)

        self._build_ui()
        if not self._embedded:
            self.device.on_status = self.statusSignal.emit
        self.searchDone.connect(self._search_finished)

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(250)
        self._ui_timer.timeout.connect(self._refresh_ui)
        self._ui_timer.start()
        self._last_connected = self.device.connected
        self._set_connected_ui(self._last_connected)

    # ── UI construction ──────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QVBoxLayout(self)

        conn = self.conn_group = QGroupBox("Connection")
        cl = QHBoxLayout(conn)
        cl.setSpacing(8)
        cl.addWidget(QLabel("Port"))
        self.port = QComboBox()
        self.port.setEditable(True)
        self.port.addItems(self._com_ports())
        self.port.setCurrentText(self.config.com_port)
        self.port.setMinimumWidth(100)
        cl.addWidget(self.port)
        self.search_btn = QPushButton("Search…")
        self.search_btn.setToolTip(
            "Probe every COM port with the read-only '?' query and "
            "select the one where a Heise indicator answers")
        self.search_btn.clicked.connect(self._handle_search)
        cl.addWidget(self.search_btn)
        cl.addWidget(QLabel("Baud"))
        self.baud = QComboBox()
        self.baud.addItems([str(b) for b in BAUD_RATES])
        self.baud.setCurrentText(str(self.config.baud))
        cl.addWidget(self.baud)
        self.sim = QCheckBox("Simulate")
        self.sim.setChecked(self.config.force_sim)
        cl.addWidget(self.sim)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.clicked.connect(self._handle_connect)
        cl.addWidget(self.connect_btn)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.clicked.connect(self._handle_disconnect)
        cl.addWidget(self.disconnect_btn)
        cl.addStretch(1)
        self.lamp = QLabel("DISCONNECTED")
        self.lamp.setProperty("mono", "true")
        self.lamp.setStyleSheet(f"color: {theme.TEXT_DIM}; "
                                f"font-weight: bold;")
        cl.addWidget(self.lamp)
        root.addWidget(conn)
        if self._embedded:
            conn.hide()

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_live_tab(), "Live")
        self.tabs.addTab(self._build_history_tab(), "History")
        root.addWidget(self.tabs, 1)

    def _build_live_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        tiles = QHBoxLayout()
        self.tiles = {}
        for i, p in enumerate(self.config.ports()):
            if not p.enabled:
                continue
            tile = _PortTile(p.name, p.unit, theme.series_color(i))
            self.tiles[p.name] = tile
            tiles.addWidget(tile)
        v.addLayout(tiles)

        ctl = QGroupBox("Instrument")
        cl = QHBoxLayout(ctl)
        cl.addWidget(QLabel("Pressure unit"))
        self.unit_combo = QComboBox()
        self.unit_combo.addItems(list(PRESSURE_UNITS.values()))
        pressure_ports = [p for p in self.config.ports()
                          if p.role == "pressure"]
        if pressure_ports:
            self.unit_combo.setCurrentText(pressure_ports[0].unit)
        self.unit_combo.currentTextChanged.connect(self._unit_changed)
        cl.addWidget(self.unit_combo)
        self.zero_btn = QPushButton("Zero Pressure")
        self.zero_btn.setToolTip("ZERO the pressure port(s) at the "
                                 "current reading")
        self.zero_btn.clicked.connect(self._handle_zero)
        cl.addWidget(self.zero_btn)
        cl.addWidget(QLabel("Damping"))
        self.damp_combo = QComboBox()
        self.damp_combo.addItems(["0 (off)", "1 (low)", "2 (medium)",
                                  "3 (high)"])
        self.damp_combo.setCurrentIndex(2)
        self.damp_combo.currentIndexChanged.connect(self._damp_changed)
        cl.addWidget(self.damp_combo)
        cl.addStretch(1)
        self.batt_lbl = QLabel("battery: --")
        self.batt_lbl.setProperty("mono", "true")
        cl.addWidget(self.batt_lbl)
        v.addWidget(ctl)
        v.addStretch(1)
        return w

    def _build_history_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        self.plots = {}
        self.curves = {}
        enabled = self.config.enabled_ports()
        for i, p in enumerate(enabled):
            plot = pg.PlotWidget()
            plot.showGrid(x=True, y=True, alpha=0.3)
            plot.setLabel("left", f"{p.name} [{p.unit}]")
            if i == len(enabled) - 1:
                plot.setLabel("bottom", "seconds ago")
            self.plots[p.name] = plot
            self.curves[p.name] = plot.plot(
                pen=pg.mkPen(theme.series_color(i), width=1))
            v.addWidget(plot)
        # link time axes so the stacked plots pan/zoom together
        plots = list(self.plots.values())
        for pl in plots[1:]:
            pl.setXLink(plots[0])
        return w

    # ── COM search (comscan in a worker thread) ──────────────────────────
    def _com_ports(self) -> list:
        try:
            from serial.tools import list_ports
            ports = [p.device for p in list_ports.comports()]
        except Exception:                              # noqa: BLE001
            ports = []
        if self.config.com_port and self.config.com_port not in ports:
            ports.append(self.config.com_port)
        return ports

    def _handle_search(self) -> None:
        if self.device.connected:
            self.statusSignal.emit(
                "Disconnect before searching — the open port would "
                "answer the probe")
            return
        self.search_btn.setEnabled(False)
        self.search_btn.setText("Searching…")
        self.statusSignal.emit("Probing COM ports with '?'…")

        def run():
            from heise import comscan
            try:
                results = comscan.search()
            except Exception as exc:                   # noqa: BLE001
                log.exception("COM search failed")
                results = []
                self.statusSignal.emit(f"COM search failed: {exc}")
            self.searchDone.emit(results)

        threading.Thread(target=run, name="heise-comscan",
                         daemon=True).start()

    def _search_finished(self, results) -> None:
        self.search_btn.setEnabled(True)
        self.search_btn.setText("Search…")
        current = self.port.currentText()
        self.port.clear()
        hits = [r for r in results if r.is_heise]
        for r in results:
            self.port.addItem(r.port.device)
            self.port.setItemData(self.port.count() - 1, r.summary,
                                  Qt.ItemDataRole.ToolTipRole)
        if not results:
            self.port.addItems(self._com_ports())
        if hits:
            self.port.setCurrentText(hits[0].port.device)
            if hits[0].baud:
                self.baud.setCurrentText(str(hits[0].baud))
            self.statusSignal.emit(
                f"Heise indicator found on {hits[0].port.device} "
                f"({hits[0].port.description or 'no description'}) — "
                f"ready to Connect")
        else:
            if current:
                self.port.setCurrentText(current)
            detail = "; ".join(r.summary for r in results) or "no ports"
            self.statusSignal.emit(f"No Heise indicator found — {detail}")

    # ── actions ──────────────────────────────────────────────────────────
    def _handle_connect(self) -> None:
        self.config.com_port = (self.port.currentText().strip()
                                or self.config.com_port)
        try:
            self.config.baud = int(self.baud.currentText())
        except ValueError:
            pass
        self.config.force_sim = self.sim.isChecked()
        try:
            self.device.connect()
        except Exception as exc:                       # noqa: BLE001
            self.statusSignal.emit(f"Connect failed: {exc}")
            log.exception("connect failed")
            return
        self._last_connected = True
        self._set_connected_ui(True)
        self._refresh_battery()

    def _handle_disconnect(self) -> None:
        self.device.disconnect()
        self._last_connected = False
        self._set_connected_ui(False)

    def _unit_changed(self, unit: str) -> None:
        for p in self.config.ports():
            if p.role == "pressure":
                if self.device.connected:
                    try:
                        self.device.set_pressure_unit(unit)
                    except (HeiseError, RuntimeError) as exc:
                        self.statusSignal.emit(str(exc))
                        return
                else:
                    p.unit = unit
                break
        for p in self.config.ports():
            tile = self.tiles.get(p.name)
            if tile is not None:
                tile.set_unit(p.unit)
            plot = self.plots.get(p.name)
            if plot is not None:
                plot.setLabel("left", f"{p.name} [{p.unit}]")

    def _handle_zero(self) -> None:
        if not self.device.connected:
            self.statusSignal.emit("Connect first")
            return
        if QMessageBox.question(
                self, "Zero pressure",
                "Zero the pressure port(s) at the current reading?") \
                != QMessageBox.StandardButton.Yes:
            return
        try:
            self.device.zero("both")
        except (HeiseError, RuntimeError) as exc:
            self.statusSignal.emit(str(exc))

    def _damp_changed(self, level: int) -> None:
        if not self.device.connected:
            return
        try:
            self.device.set_damping(level)
            self.statusSignal.emit(f"Damping set to {level}")
        except (HeiseError, RuntimeError) as exc:
            self.statusSignal.emit(str(exc))

    def _refresh_battery(self) -> None:
        try:
            self.batt_lbl.setText(f"battery: {self.device.battery():.2f} V")
        except Exception:                              # noqa: BLE001
            self.batt_lbl.setText("battery: --")

    # ── periodic UI refresh (ring poll, house pattern) ───────────────────
    def _set_connected_ui(self, connected: bool) -> None:
        self.connect_btn.setEnabled(not connected)
        self.disconnect_btn.setEnabled(connected)
        for w in (self.port, self.baud, self.sim, self.search_btn):
            w.setEnabled(not connected)
        if connected:
            mode = "SIMULATION" if self.device.sim_mode else "LIVE"
            color = theme.WARNING if self.device.sim_mode \
                else theme.SUCCESS
            self.lamp.setText(mode)
            self.lamp.setStyleSheet(f"color: {color}; "
                                    f"font-weight: bold;")
        else:
            self.lamp.setText("DISCONNECTED")
            self.lamp.setStyleSheet(f"color: {theme.TEXT_DIM}; "
                                    f"font-weight: bold;")

    def _refresh_ui(self) -> None:
        # track connection changes made OUTSIDE the panel's own buttons
        # (embedded hosts) so the lamp/controls come alive / lock down
        # without a Connect click.
        connected = self.device.connected
        if connected != self._last_connected:
            self._last_connected = connected
            self._set_connected_ui(connected)
            if connected:
                self._refresh_battery()
        if not connected or self.device.ring is None:
            return
        latest = self.device.latest()
        if latest:
            for name, tile in self.tiles.items():
                tile.set_value(latest.get(name))
        # history: last plot_window seconds from the ring
        window_s = 120.0
        n = int(window_s / max(self.config.poll_s, 0.05)) + 2
        names = list(self.curves)
        data = self.device.ring.tail(n, fields=["t"] + names)
        if data["t"].size >= 2:
            x = data["t"] - data["t"][-1]
            for name in names:
                self.curves[name].setData(x, data[name])

    def closeEvent(self, event) -> None:              # noqa: N802
        self._ui_timer.stop()
        super().closeEvent(event)


class HeiseMainWindow(QMainWindow):
    def __init__(self, cfg: Optional[HeiseConfig] = None):
        super().__init__()
        self.setWindowTitle("Heise PM Indicator — pressure / temperature")
        self.resize(820, 560)
        self.setStyleSheet(theme.get_stylesheet())
        self.panel = HeisePanel(cfg, self)
        self.setCentralWidget(self.panel)

        sb = QStatusBar()
        self.setStatusBar(sb)
        self._sb_status = QLabel("Idle")
        self._sb_status.setProperty("mono", "true")
        sb.addPermanentWidget(self._sb_status, 1)
        self.panel.statusSignal.connect(self._sb_status.setText)

        m = self.menuBar().addMenu("&File")
        act = QAction("&Save config…", self)
        act.triggered.connect(self._save_config)
        m.addAction(act)
        act = QAction("&Load config…", self)
        act.triggered.connect(self._load_config)
        m.addAction(act)
        m.addSeparator()
        act = QAction("E&xit", self)
        act.triggered.connect(self.close)
        m.addAction(act)

    def _save_config(self) -> None:
        path, _f = QFileDialog.getSaveFileName(
            self, "Save config", "heise_config.json", "JSON (*.json)")
        if path:
            self.panel.config.save(path)
            self._sb_status.setText(f"Config saved to {path}")

    def _load_config(self) -> None:
        path, _f = QFileDialog.getOpenFileName(
            self, "Load config", "", "JSON (*.json)")
        if not path:
            return
        if self.panel.device.connected:
            self._sb_status.setText("Disconnect before loading a config")
            return
        cfg = HeiseConfig.load(path)
        self.panel.config = cfg
        self.panel.device.config = cfg
        self.panel.port.setCurrentText(cfg.com_port)
        self.panel.baud.setCurrentText(str(cfg.baud))
        self.panel.sim.setChecked(cfg.force_sim)
        self._sb_status.setText(f"Config loaded from {path}")

    def closeEvent(self, event) -> None:              # noqa: N802
        if self.panel.device.connected:
            self.panel.device.disconnect()
        super().closeEvent(event)
