"""Balance calibration window — Python port of the MATLAB ForceCal app.

Tabs mirror the .mlapp: Measurement Setup (device + channels + balance
info), Calibration Procedure (orientation, guide image, moment arm,
timed acquire, measurement table, write .vol), Time History (last
acquisition), plus a Cal Summary tab that runs the same least-squares
reduction the consumers use and reports R^2 / bias per element.

Can run standalone (owns its DAQ connection) or embedded — pass
``device=`` with a live NiUsb6351/Strainbook616 (freestream's balance
device) and it will share that stream instead of opening a second one.
"""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
from PyQt6.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QBrush, QColor, QPixmap
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit, QPushButton,
    QSplitter, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
    QWidget)

import pyqtgraph as pg

from .. import theme
from ..daq import BACKENDS, BalanceDaq, list_ni_devices
from ..diagnostics import (OUTLIER_Z, channel_trend, diagnose,
                           diagnostics_text)
from ..report import report_text, summarize
from ..session import BalanceKind, CalSession, TestPoint
from ..volfile import read_vol_session, validate_session, write_vol

IMAGES_DIR = Path(__file__).resolve().parents[2] / "FB_Cal_GUI"

CAL_TYPES = ("Linear", "Quadratic", "Cubic")


from dataclasses import dataclass


@dataclass
class _ChannelSel:
    """One channel VALUE selected in the off-diagonal inspector."""
    key: str
    index: int
    channel: int
    channel_name: str
    load: float
    measured: float
    expected: float
    z: float
    flagged: bool


class _AcquireThread(QThread):
    """Blocking timed acquisition off the GUI thread."""
    done = pyqtSignal(object)
    fail = pyqtSignal(str)

    def __init__(self, daq: BalanceDaq, seconds: float, kind: BalanceKind,
                 parent=None):
        super().__init__(parent)
        self._daq, self._seconds, self._kind = daq, seconds, kind

    def run(self) -> None:
        try:
            self.done.emit(self._daq.acquire(self._seconds, self._kind))
        except Exception as exc:                       # noqa: BLE001
            self.fail.emit(str(exc))


class BalanceCalWindow(QMainWindow):
    statusSignal = pyqtSignal(str)

    def __init__(self, device=None, backend: str = "ni6351",
                 sim: bool = False, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Balance Calibration — .vol acquisition")
        self.resize(1000, 700)
        self.setStyleSheet(theme.get_stylesheet())
        theme.apply_pyqtgraph_theme()

        self.session = CalSession()
        self.daq: Optional[BalanceDaq] = None
        self._external_device = device
        self._acquire_thread: Optional[_AcquireThread] = None
        self._active_kind: Optional[BalanceKind] = None
        self._pending_key: str = ""
        self._pending_load: float = 0.0
        self._last_vol_path: str = ""
        self._default_backend = backend
        self._default_sim = sim

        self.statusSignal.connect(self._show_status)

        tabs = QTabWidget()
        tabs.addTab(self._build_setup_tab(), "Measurement Setup")
        tabs.addTab(self._build_procedure_tab(), "Calibration Procedure")
        tabs.addTab(self._build_history_tab(), "Time History")
        tabs.addTab(self._build_summary_tab(), "Cal Summary")
        self.tabs = tabs
        self.setCentralWidget(tabs)
        self._build_menus()

        self._live_timer = QTimer(self)
        self._live_timer.setInterval(250)
        self._live_timer.timeout.connect(self._refresh_live)

        if self._external_device is not None:
            self.backend_combo.setEnabled(False)
            self.sim_check.setEnabled(False)
            self.ni_dev_combo.setEnabled(False)
            # the shared device's live channel layout is authoritative —
            # changing it here would rename freestream's channels
            layout = getattr(getattr(device, "config", None),
                             "balance_config", "Force")
            self.balance_type_combo.setCurrentText(
                BalanceKind.FORCE.value if layout == "Force"
                else BalanceKind.MOMENT.value)
            self.balance_type_combo.setEnabled(False)

        self._on_balance_type_changed()

    # ── Measurement Setup tab ────────────────────────────────────────────
    def _build_setup_tab(self) -> QWidget:
        w = QWidget()
        grid = QGridLayout(w)

        dev_box = QGroupBox("DAQ Device")
        form = QFormLayout(dev_box)
        self.backend_combo = QComboBox()
        for key, label in BACKENDS.items():
            self.backend_combo.addItem(label, key)
        idx = self.backend_combo.findData(self._default_backend)
        self.backend_combo.setCurrentIndex(max(idx, 0))
        form.addRow("Driver", self.backend_combo)

        dev_row = QHBoxLayout()
        self.ni_dev_combo = QComboBox()
        self.ni_dev_combo.setEditable(True)
        refresh = QPushButton("Search")
        refresh.clicked.connect(self._refresh_devices)
        dev_row.addWidget(self.ni_dev_combo, 1)
        dev_row.addWidget(refresh)
        form.addRow("Device", dev_row)

        self.sim_check = QCheckBox("Simulate (no hardware)")
        self.sim_check.setChecked(self._default_sim)
        form.addRow("", self.sim_check)

        self.rate_spin = QDoubleSpinBox()
        self.rate_spin.setRange(10.0, 100_000.0)
        self.rate_spin.setValue(1000.0)
        self.rate_spin.setSuffix(" Hz")
        form.addRow("Scan rate", self.rate_spin)

        self.balance_type_combo = QComboBox()
        self.balance_type_combo.addItems([k.value for k in BalanceKind])
        self.balance_type_combo.currentTextChanged.connect(
            self._on_balance_type_changed)
        form.addRow("Balance type", self.balance_type_combo)
        self.backend_combo.currentIndexChanged.connect(
            lambda *_a: self._preview_channels())

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.clicked.connect(self._toggle_connect)
        form.addRow("", self.connect_btn)

        self.panel_btn = QPushButton("Device Panel…")
        self.panel_btn.setToolTip(
            "Open the driver's native app (live tiles, channels, "
            "output/trigger) sharing this connection")
        self.panel_btn.clicked.connect(self._open_device_panel)
        form.addRow("", self.panel_btn)

        self.lamp = QLabel("DISCONNECTED")
        self.lamp.setProperty("mono", "true")
        form.addRow("Status", self.lamp)

        chan_box = QGroupBox("Channels (edit physical / range before "
                             "connecting)")
        cv = QVBoxLayout(chan_box)
        self.chan_table = QTableWidget(0, 3)
        self.chan_table.setHorizontalHeaderLabels(
            ["Name", "Physical", "Range"])
        self.chan_table.verticalHeader().setVisible(False)
        cv.addWidget(self.chan_table)

        info_box = QGroupBox("Balance Info — distance is element to "
                             "balance center")
        iv = QGridLayout(info_box)
        self.max_table = QTableWidget(6, 3)
        self.max_table.setHorizontalHeaderLabels(
            ["Element", "Max Load", "Distance [in]"])
        self.max_table.verticalHeader().setVisible(False)
        iv.addWidget(self.max_table, 0, 0)

        meta = QWidget()
        mform = QFormLayout(meta)
        self.operator_edit = QLineEdit()
        self.serial_edit = QLineEdit()
        self.diameter_edit = QLineEdit("0.75 in.")
        self.date_edit = QLineEdit(date.today().isoformat())
        mform.addRow("Cal'd by", self.operator_edit)
        mform.addRow("Serial #", self.serial_edit)
        mform.addRow("Outer diameter", self.diameter_edit)
        mform.addRow("Date", self.date_edit)
        iv.addWidget(meta, 0, 1)

        grid.addWidget(dev_box, 0, 0)
        grid.addWidget(info_box, 0, 1)
        grid.addWidget(chan_box, 1, 0, 1, 2)
        grid.setRowStretch(1, 1)
        grid.setColumnStretch(1, 1)
        return w

    # ── Calibration Procedure tab ────────────────────────────────────────
    def _build_procedure_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)

        top = QHBoxLayout()
        left = QFormLayout()
        self.orient_combo = QComboBox()
        self.orient_combo.currentTextChanged.connect(
            self._on_orientation_changed)
        left.addRow("Load orientation", self.orient_combo)

        self.arm_spin = QDoubleSpinBox()
        self.arm_spin.setDecimals(4)
        self.arm_spin.setRange(0.0001, 1000.0)
        self.arm_spin.setValue(1.0)
        left.addRow("Moment arm [in]", self.arm_spin)

        self.seconds_spin = QDoubleSpinBox()
        self.seconds_spin.setRange(0.1, 60.0)
        self.seconds_spin.setValue(1.0)
        self.seconds_spin.setSuffix(" s")
        left.addRow("Average over", self.seconds_spin)

        self.live_label = QLabel("—")
        self.live_label.setProperty("mono", "true")
        left.addRow("Live volts", self.live_label)
        top.addLayout(left)

        self.image_label = QLabel()
        self.image_label.setMinimumSize(420, 130)
        self.image_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top.addWidget(self.image_label, 1)
        v.addLayout(top)

        self.mtable = QTableWidget(0, 8)
        self.mtable.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.mtable.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.mtable.verticalHeader().setVisible(False)

        # live cal plot: primary-axis load vs measured bridge volts,
        # std error bars flag noisy (bad) points at a glance
        self.cal_plot = pg.PlotWidget()
        self.cal_plot.showGrid(x=True, y=True, alpha=0.3)
        self.cal_plot.addLegend(offset=(10, 10))

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.mtable)
        split.addWidget(self.cal_plot)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        v.addWidget(split, 1)

        btns = QHBoxLayout()
        self.acquire_btn = QPushButton("Acquire Test Point")
        self.acquire_btn.setObjectName("primary")
        self.acquire_btn.clicked.connect(self._acquire_point)
        btns.addWidget(self.acquire_btn)
        rm = QPushButton("Delete Selected Row")
        rm.setObjectName("danger")
        rm.clicked.connect(self._delete_row)
        btns.addWidget(rm)
        btns.addStretch(1)
        self.count_label = QLabel("0 points")
        btns.addWidget(self.count_label)
        write = QPushButton("Write to File…")
        write.setObjectName("success")
        write.clicked.connect(self._write_vol)
        btns.addWidget(write)
        v.addLayout(btns)
        return w

    # ── Time History tab ─────────────────────────────────────────────────
    def _build_history_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        # per-channel visibility toggles (checkboxes appear as channels
        # show up in acquisitions; state persists across acquisitions)
        self._hist_check_row = QHBoxLayout()
        self._hist_check_row.setSpacing(10)
        lbl = QLabel("Show")
        lbl.setObjectName("dim")
        self._hist_check_row.addWidget(lbl)
        self._hist_check_row.addStretch(1)
        self._hist_checks = {}
        self._hist_visible = {}          # name -> bool (default True)
        self._hist_curves = {}
        v.addLayout(self._hist_check_row)
        self.plot = pg.PlotWidget()
        self.plot.setLabel("bottom", "Time [s]")
        self.plot.setLabel("left", "Voltage [V]")
        self.plot.addLegend()
        self.plot.showGrid(x=True, y=True, alpha=0.3)
        v.addWidget(self.plot)
        return w

    def _hist_toggle(self, name: str, on: bool) -> None:
        self._hist_visible[name] = bool(on)
        curve = self._hist_curves.get(name)
        if curve is not None:
            curve.setVisible(bool(on))

    def _ensure_hist_check(self, name: str, color: str) -> None:
        if name in self._hist_checks:
            return
        chk = QCheckBox(name)
        chk.setChecked(self._hist_visible.get(name, True))
        chk.setStyleSheet(f"QCheckBox {{ color: {color}; "
                          f"font-weight: bold; }}")
        chk.setToolTip(f"Show/hide {name} on the plot")
        chk.toggled.connect(lambda on, n=name: self._hist_toggle(n, on))
        self._hist_checks[name] = chk
        # insert before the trailing stretch
        self._hist_check_row.insertWidget(
            self._hist_check_row.count() - 1, chk)

    # ── Cal Summary tab ──────────────────────────────────────────────────
    def _build_summary_tab(self) -> QWidget:
        w = QWidget()
        v = QVBoxLayout(w)
        row = QHBoxLayout()
        row.addWidget(QLabel("Fit type"))
        self.cal_type_combo = QComboBox()
        self.cal_type_combo.addItems(CAL_TYPES)
        row.addWidget(self.cal_type_combo)
        compute = QPushButton("Compute Calibration")
        compute.setObjectName("primary")
        compute.clicked.connect(self._compute_summary)
        row.addWidget(compute)
        save = QPushButton("Save Report…")
        save.clicked.connect(self._save_report)
        row.addStretch(1)
        row.addWidget(save)
        v.addLayout(row)

        self.summary_text = QPlainTextEdit()
        self.summary_text.setReadOnly(True)
        self.summary_text.setProperty("mono", "true")

        # interactive diagnostics: applied vs predicted per element,
        # outliers ringed, points clickable → exclude/delete + refit
        diag_panel = QWidget()
        dv = QVBoxLayout(diag_panel)
        dr = QHBoxLayout()
        dr.addWidget(QLabel("Element"))
        self.diag_element_combo = QComboBox()
        self.diag_element_combo.currentIndexChanged.connect(
            lambda *_a: self._refresh_diag_plot())
        dr.addWidget(self.diag_element_combo)
        self.diag_view_combo = QComboBox()
        self.diag_view_combo.addItems(["Applied vs Predicted",
                                       "Residuals vs Applied",
                                       "Channel volts (off-diagonal)"])
        self.diag_view_combo.currentIndexChanged.connect(
            self._diag_view_changed)
        dr.addWidget(self.diag_view_combo)
        self.diag_section_combo = QComboBox()
        self.diag_section_combo.setToolTip(
            "Section whose channel voltages to inspect")
        self.diag_section_combo.currentIndexChanged.connect(
            lambda *_a: self._refresh_diag_plot())
        self.diag_section_combo.hide()
        dr.addWidget(self.diag_section_combo)
        dr.addStretch(1)
        dv.addLayout(dr)

        self.diag_plot = pg.PlotWidget()
        self.diag_plot.showGrid(x=True, y=True, alpha=0.3)
        self.diag_plot.addLegend(offset=(10, 10))
        dv.addWidget(self.diag_plot, 1)

        self.diag_detail = QLabel("Compute, then click a point to "
                                  "inspect it.")
        self.diag_detail.setProperty("mono", "true")
        self.diag_detail.setWordWrap(True)
        dv.addWidget(self.diag_detail)

        br = QHBoxLayout()
        self.diag_exclude_btn = QPushButton("Exclude && Refit")
        self.diag_exclude_btn.setObjectName("danger")
        self.diag_exclude_btn.clicked.connect(self._diag_exclude)
        self.diag_include_btn = QPushButton("Re-include All Excluded")
        self.diag_include_btn.clicked.connect(self._diag_include_all)
        self.diag_delete_btn = QPushButton("Delete Point && Refit")
        self.diag_delete_btn.clicked.connect(self._diag_delete)
        self.diag_goto_btn = QPushButton("Show in Table")
        self.diag_goto_btn.clicked.connect(self._diag_goto)
        # value-level repair (channel view): fix ONE voltage without
        # losing the row — for off-diagonal glitches
        self.diag_repair_btn = QPushButton("Repair Value from Trend")
        self.diag_repair_btn.setObjectName("primary")
        self.diag_repair_btn.setToolTip(
            "Replace this single channel voltage with the section's "
            "robust trend value and refit — keeps the row")
        self.diag_repair_btn.clicked.connect(self._diag_repair_value)
        self.diag_edit_btn = QPushButton("Edit Value…")
        self.diag_edit_btn.setToolTip(
            "Manually enter a replacement for this channel voltage")
        self.diag_edit_btn.clicked.connect(self._diag_edit_value)
        for b in (self.diag_exclude_btn, self.diag_delete_btn,
                  self.diag_goto_btn, self.diag_repair_btn,
                  self.diag_edit_btn):
            b.setEnabled(False)
        br.addWidget(self.diag_repair_btn)
        br.addWidget(self.diag_edit_btn)
        br.addWidget(self.diag_exclude_btn)
        br.addWidget(self.diag_delete_btn)
        br.addWidget(self.diag_goto_btn)
        br.addStretch(1)
        br.addWidget(self.diag_include_btn)
        dv.addLayout(br)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(self.summary_text)
        split.addWidget(diag_panel)
        split.setStretchFactor(0, 2)
        split.setStretchFactor(1, 3)
        v.addWidget(split, 1)
        return w

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")
        act = QAction("&Load .vol for editing…", self)
        act.setToolTip("Reload a calibration to modify points / append")
        act.triggered.connect(self._load_vol)
        file_menu.addAction(act)
        act = QAction("&Write .vol…", self)
        act.triggered.connect(self._write_vol)
        file_menu.addAction(act)
        act = QAction("&Summarize existing .vol…", self)
        act.triggered.connect(self._summarize_existing)
        file_menu.addAction(act)
        file_menu.addSeparator()
        act = QAction("Open &device panel…", self)
        act.triggered.connect(self._open_device_panel)
        file_menu.addAction(act)
        file_menu.addSeparator()
        act = QAction("E&xit", self)
        act.triggered.connect(self.close)
        file_menu.addAction(act)

    # ── device handling ──────────────────────────────────────────────────
    @property
    def kind(self) -> BalanceKind:
        return (BalanceKind.FORCE
                if self.balance_type_combo.currentText()
                == BalanceKind.FORCE.value else BalanceKind.MOMENT)

    def _refresh_devices(self) -> None:
        self.ni_dev_combo.clear()
        names = list_ni_devices()
        if names:
            self.ni_dev_combo.addItems(names)
        else:
            self._show_status("No NI devices found (is NI-DAQmx "
                              "installed?) — try Simulate")

    def _toggle_connect(self) -> None:
        if self.daq is not None and self.daq.connected:
            self._disconnect()
        else:
            self._connect()

    def _connect(self) -> None:
        backend = self.backend_combo.currentData()
        try:
            if self._external_device is not None:
                self.daq = BalanceDaq(backend,
                                      driver=self._external_device)
            else:
                self.daq = BalanceDaq(
                    backend,
                    device_name=self.ni_dev_combo.currentText().strip(),
                    sim=self.sim_check.isChecked(),
                    scan_hz=self.rate_spin.value())
                self.daq.driver.config.set_balance_config(
                    "Force" if self.kind is BalanceKind.FORCE
                    else "Moment")
                self._apply_channel_edits(backend)
            self.daq.on_status = self.statusSignal.emit
            self.daq.connect(self.kind)
        except Exception as exc:                       # noqa: BLE001
            self.daq = None
            QMessageBox.critical(self, "Connect failed", str(exc))
            return
        self.connect_btn.setText("Disconnect")
        self.balance_type_combo.setEnabled(False)
        mode = "SIMULATION" if self.daq.sim_mode else "ACQUIRING"
        color = theme.WARNING if self.daq.sim_mode else theme.SUCCESS
        self.lamp.setText(mode)
        self.lamp.setStyleSheet(f"color: {color}; font-weight: bold;")
        self._populate_channel_table()
        self._live_timer.start()

    def _disconnect(self) -> None:
        self._live_timer.stop()
        if self.daq is not None:
            self.daq.disconnect()
        self.daq = None
        self.connect_btn.setText("Connect")
        self.balance_type_combo.setEnabled(True)
        self.lamp.setText("DISCONNECTED")
        self.lamp.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.live_label.setText("—")

    def _apply_channel_edits(self, backend: str) -> None:
        """Push Physical/Range cells into the driver config pre-connect."""
        if self.chan_table.rowCount() == 0:
            return
        cfg = self.daq.driver.config
        by_name = {c.name: c for c in cfg.enabled_channels()}
        for r in range(self.chan_table.rowCount()):
            name = self.chan_table.item(r, 0)
            phys = self.chan_table.item(r, 1)
            rng = self.chan_table.item(r, 2)
            ch = by_name.get(name.text() if name else "")
            if ch is None:
                continue
            try:
                if phys and phys.text().strip():
                    ch.channel = int(re.sub(r"[^\d]", "",
                                            phys.text().strip()))
                if rng and rng.text().strip():
                    # accept the table's own display format ("±0.2 V",
                    # "±11 mV") as well as "0.2" or "-0.2,0.2"
                    m = re.findall(r"[-+]?\d*\.?\d+", rng.text())
                    if not m:
                        raise ValueError(rng.text())
                    span = abs(float(m[-1]))
                    if backend == "ni6351":
                        ch.v_min, ch.v_max = -span, span
                    else:
                        ch.range_mv = span
            except ValueError:
                self._show_status(f"Ignored bad channel entry row {r + 1}")

    def _preview_channels(self) -> None:
        """Show the default channel layout before any connection so the
        physical/range cells can be edited up front."""
        if self.daq is not None and self.daq.connected:
            return
        backend = self.backend_combo.currentData()
        try:
            tmp = BalanceDaq(backend, sim=True)
            cfg_kind = ("Force" if self.kind is BalanceKind.FORCE
                        else "Moment")
            tmp.driver.config.set_balance_config(cfg_kind)
            self._populate_channel_table(
                tmp.driver.config.enabled_channels(), backend)
        except Exception as exc:                       # noqa: BLE001
            self._show_status(f"Channel preview unavailable: {exc}")

    def _populate_channel_table(self, chans=None, backend=None) -> None:
        if chans is None:
            chans = (self.daq.driver.config.enabled_channels()
                     if self.daq is not None else [])
        if backend is None:
            backend = self.backend_combo.currentData()
        self.chan_table.setRowCount(len(chans))
        for r, ch in enumerate(chans):
            if backend == "ni6351":
                phys = getattr(ch, "physical", f"ai{ch.channel}")
                rng = f"±{getattr(ch, 'native_range', 0):g} V"
            else:
                phys = f"CH{ch.channel}"
                rng = f"±{getattr(ch, 'range_mv', 0):g} mV"
            for c, text in enumerate((ch.name, str(phys), rng)):
                item = QTableWidgetItem(text)
                if c == 0:
                    item.setFlags(item.flags()
                                  & ~Qt.ItemFlag.ItemIsEditable)
                self.chan_table.setItem(r, c, item)
        self.chan_table.resizeColumnsToContents()

    # ── balance type / orientation ───────────────────────────────────────
    def _on_balance_type_changed(self, *_a) -> None:
        s = self.session
        if self.kind is getattr(self, "_active_kind", None):
            return
        if s.point_count() > 0:
            if QMessageBox.question(
                    self, "Discard test points?",
                    f"Switching balance type discards the "
                    f"{s.point_count()} acquired test point(s). "
                    f"Continue?") != QMessageBox.StandardButton.Yes:
                self.balance_type_combo.blockSignals(True)
                self.balance_type_combo.setCurrentText(
                    self._active_kind.value)
                self.balance_type_combo.blockSignals(False)
                return
        self._active_kind = self.kind
        s.kind = self.kind
        s.points.clear()
        # fresh Max Load / Distance cells — stale Force-balance numbers
        # must not be silently re-attributed to Moment elements
        self.max_table.setRowCount(6)
        for r, el in enumerate(s.elements):
            item = QTableWidgetItem(el.name)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.max_table.setItem(r, 0, item)
            self.max_table.setItem(r, 1, QTableWidgetItem(""))
            self.max_table.setItem(r, 2, QTableWidgetItem(""))
        self.orient_combo.blockSignals(True)
        self.orient_combo.clear()
        self.orient_combo.addItems([o.key for o in s.orientations])
        self.orient_combo.blockSignals(False)
        self._preview_channels()
        self._on_orientation_changed()

    def _sync_session_meta(self) -> None:
        s = self.session
        s.operator = self.operator_edit.text().strip()
        s.serial_number = self.serial_edit.text().strip()
        s.outer_diameter = self.diameter_edit.text().strip()
        try:
            s.cal_date = date.fromisoformat(self.date_edit.text().strip())
        except ValueError:
            s.cal_date = date.today()
        s.max_loads.clear()
        s.distances.clear()
        for r, el in enumerate(s.elements):
            load_item = self.max_table.item(r, 1)
            dist_item = self.max_table.item(r, 2)
            try:
                if load_item and load_item.text().strip():
                    s.max_loads[el.name] = float(load_item.text())
            except ValueError:
                pass
            try:
                if dist_item and dist_item.text().strip():
                    d = float(dist_item.text())
                    if el.distance_tag:
                        s.distances[el.distance_tag] = d
                    elif el.name == "Mx":
                        s.distances["roll_arm"] = d
            except ValueError:
                pass

    def _current_key(self) -> str:
        return self.orient_combo.currentText()

    def _on_orientation_changed(self, *_a) -> None:
        key = self._current_key()
        if not key:
            return
        self._sync_session_meta()
        s = self.session
        orient = s.orientation(key)
        self.arm_spin.setValue(s.moment_arm(key))
        warn = s.moment_arm_warning(key)
        if warn:
            self._show_status(f"WARNING: {warn}")
        self.acquire_btn.setText(f"Acquire Test Point — [{key}]")

        img = IMAGES_DIR / orient.image_name(s.kind)
        if img.exists():
            pix = QPixmap(str(img))
            self.image_label.setPixmap(pix.scaled(
                self.image_label.size(),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))
        else:
            self.image_label.setText(f"(no guide image: {img.name})")

        cols = ([f"Load [{orient.element.load_unit}]"]
                + [el.channel for el in s.elements] + ["Excitation"])
        self.mtable.setColumnCount(len(cols))
        self.mtable.setHorizontalHeaderLabels(cols)
        self._refresh_mtable()

    def _refresh_mtable(self) -> None:
        key = self._current_key()
        points = self.session.points.get(key, [])
        self.mtable.setRowCount(len(points))
        dim = QBrush(QColor(theme.TEXT_DISABLED))
        for r, p in enumerate(points):
            vals = [f"{p.load:g}" + (" (excl)" if p.excluded else "")] \
                + [f"{v:.6e}" for v in p.volts] \
                + [f"{p.excitation:.4f}"]
            for c, text in enumerate(vals):
                item = QTableWidgetItem(text)
                if p.excluded:
                    item.setForeground(dim)
                self.mtable.setItem(r, c, item)
        self.count_label.setText(
            f"{self.session.point_count()} points total, "
            f"{len(points)} in [{key}]")
        self._refresh_cal_plot()

    #: a point is flagged noisy when its primary-channel std exceeds
    #: this multiple of the orientation's median std (needs >= 3 points)
    _NOISY_STD_FACTOR = 3.0

    def _refresh_cal_plot(self) -> None:
        """Load vs primary-channel volts for the current orientation,
        with +/- std error bars — noisy points drawn in warning color."""
        key = self._current_key()
        if not key:
            return
        s = self.session
        orient = s.orientation(key)
        idx = [el.channel for el in s.elements].index(
            orient.element.channel)
        points = s.points.get(key, [])

        self.cal_plot.clear()
        self.cal_plot.setLabel(
            "bottom", f"Load [{orient.element.load_unit}]")
        self.cal_plot.setLabel(
            "left", f"{orient.element.channel} [V]")
        self.cal_plot.setTitle(f"[{key}] — {len(points)} points")
        if not points:
            return

        loads = np.array([p.load for p in points])
        volts = np.array([p.volts[idx] for p in points])
        stds = np.array([(p.stds[idx] if p.stds else 0.0)
                         for p in points])

        # error bars (std of the averaged samples = noise during the
        # dwell; tall bars mean the weight was still swinging)
        has_std = stds > 0
        if np.any(has_std):
            span = max(float(loads.max() - loads.min()), 1e-9)
            self.cal_plot.addItem(pg.ErrorBarItem(
                x=loads[has_std], y=volts[has_std],
                top=stds[has_std], bottom=stds[has_std],
                beam=0.01 * span, pen=pg.mkPen(theme.TEXT_DIM)))

        med = float(np.median(stds[has_std])) if np.any(has_std) else 0.0
        noisy = (has_std & (stds > self._NOISY_STD_FACTOR * med)
                 if np.count_nonzero(has_std) >= 3 else
                 np.zeros_like(has_std))

        good = ~noisy
        if np.any(good):
            self.cal_plot.plot(
                loads[good], volts[good], pen=None, symbol="o",
                symbolSize=7, symbolBrush=theme.ACCENT_LIGHT,
                symbolPen=None, name="points")
        if np.any(noisy):
            self.cal_plot.plot(
                loads[noisy], volts[noisy], pen=None, symbol="o",
                symbolSize=9, symbolBrush=theme.WARNING,
                symbolPen=pg.mkPen(theme.ERROR, width=1),
                name=f"noisy (std > {self._NOISY_STD_FACTOR:g}x med)")
            worst = np.argmax(stds)
            self._show_status(
                f"Noisy point(s) in [{key}] — worst at load "
                f"{loads[worst]:g} (std {stds[worst]:.2e} V); "
                f"consider deleting and re-acquiring")
        # linear trend through the points as a quick sanity reference
        if len(points) >= 2 and float(np.ptp(loads)) > 0:
            coef = np.polyfit(loads, volts, 1)
            xs = np.array([loads.min(), loads.max()])
            self.cal_plot.plot(xs, np.polyval(coef, xs),
                               pen=pg.mkPen(theme.TEXT_DIM, width=1,
                                            style=Qt.PenStyle.DashLine))

    # ── acquisition ──────────────────────────────────────────────────────
    def _acquire_point(self) -> None:
        if self.daq is None or not self.daq.connected:
            QMessageBox.warning(self, "Not connected",
                                "Connect a DAQ on the Measurement Setup "
                                "tab first (or enable Simulate).")
            return
        if self._acquire_thread is not None:
            return
        key = self._current_key()
        self._sync_session_meta()
        orient = self.session.orientation(key)
        unit = "lb" if orient.element.load_unit == "lb" else "lb (weight)"
        weight, ok = QInputDialog.getDouble(
            self, "Applied load",
            f"[{key}]  applied dead weight [{unit}]:",
            0.0, -1e6, 1e6, 4)
        if not ok:
            return
        load = self._compute_load(weight)
        limit = self.session.max_loads.get(orient.element.name)
        if limit and abs(load) > limit:
            if QMessageBox.question(
                    self, "Over limit",
                    f"{load:g} {orient.element.load_unit} exceeds the "
                    f"rated {limit:g} — acquire anyway?") \
                    != QMessageBox.StandardButton.Yes:
                return
        # capture load AND orientation now: the combo could be switched
        # while the worker averages, and the point belongs to this one
        self._pending_load = load
        self._pending_key = key
        self.acquire_btn.setEnabled(False)
        self.orient_combo.setEnabled(False)
        self.acquire_btn.setText(f"Averaging {self.seconds_spin.value():g}"
                                 f" s — [{key}]")
        self._acquire_thread = _AcquireThread(
            self.daq, self.seconds_spin.value(), self.session.kind, self)
        self._acquire_thread.done.connect(self._acquire_done)
        self._acquire_thread.fail.connect(self._acquire_failed)
        self._acquire_thread.finished.connect(self._acquire_cleanup)
        self._acquire_thread.start()

    def _compute_load(self, weight: float) -> float:
        """Entered dead weight x current moment arm."""
        return weight * self.arm_spin.value()

    def _acquire_done(self, acq) -> None:
        key = self._pending_key or self._current_key()
        s = self.session
        bridges = [el.channel for el in s.elements]
        point = TestPoint(
            load=self._pending_load,
            volts=[acq.means[b] for b in bridges],
            excitation=acq.means["Excitation"],
            stds=[acq.stds.get(b, 0.0) for b in bridges])
        s.add_point(key, point)
        if abs(point.excitation) < 0.5:
            self._show_status("WARNING: excitation reads "
                              f"{point.excitation:.3f} V — check supply")
        self._refresh_mtable()
        self._plot_acquisition(acq)
        self._show_status(f"Acquired [{key}] load {point.load:g} "
                          f"({acq.rate_hz:g} Hz x {acq.seconds:g} s)")

    def _acquire_failed(self, msg: str) -> None:
        QMessageBox.critical(self, "Acquisition failed", msg)

    def _acquire_cleanup(self) -> None:
        self._acquire_thread = None
        self._pending_key = ""
        self.acquire_btn.setEnabled(True)
        self.orient_combo.setEnabled(True)
        # restore the button label WITHOUT re-running the orientation
        # handler: that would reset a manually entered moment arm
        self.acquire_btn.setText(
            f"Acquire Test Point — [{self._current_key()}]")
        self._refresh_mtable()

    def _plot_acquisition(self, acq) -> None:
        self.plot.clear()
        self._hist_curves = {}
        if acq.t.size == 0:
            return
        t = acq.t - acq.t[0]
        for i, (name, v) in enumerate(acq.volts.items()):
            if name == "Excitation":
                continue
            color = theme.series_color(i)
            curve = self.plot.plot(t, v, pen=pg.mkPen(color, width=1),
                                   name=name)
            self._hist_curves[name] = curve
            self._ensure_hist_check(name, color)
            curve.setVisible(self._hist_visible.get(name, True))

    def _refresh_live(self) -> None:
        if self.daq is None or not self.daq.connected:
            return
        vals = self.daq.latest_volts(self.session.kind)
        self.live_label.setText("  ".join(
            f"{n}:{v:+.4f}" for n, v in vals.items()))

    # ── table edit / output ──────────────────────────────────────────────
    def _delete_row(self) -> None:
        key = self._current_key()
        rows = sorted({i.row() for i in self.mtable.selectedIndexes()},
                      reverse=True)
        if not rows:
            return
        if QMessageBox.question(
                self, "Delete rows",
                f"Delete {len(rows)} selected row(s) from [{key}]?") \
                != QMessageBox.StandardButton.Yes:
            return
        for r in rows:
            self.session.remove_point(key, r)
        self._refresh_mtable()

    def _load_vol(self) -> None:
        """Reload an existing .vol into the session so points can be
        inspected, deleted, and new ones appended, then rewritten."""
        if self.session.point_count() > 0:
            if QMessageBox.question(
                    self, "Replace session?",
                    f"Loading a file replaces the current "
                    f"{self.session.point_count()} test point(s). "
                    f"Continue?") != QMessageBox.StandardButton.Yes:
                return
        path, _f = QFileDialog.getOpenFileName(
            self, "Load .vol for editing", "", "Voltage cal (*.vol)")
        if not path:
            return
        try:
            loaded = read_vol_session(path)
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.critical(self, "Load failed", str(exc))
            return
        if (loaded.kind is not self.kind
                and (self._external_device is not None
                     or (self.daq is not None and self.daq.connected))):
            QMessageBox.warning(
                self, "Balance type mismatch",
                f"{path} is a {loaded.kind.value} calibration but the "
                f"DAQ in use is configured for a {self.kind.value}. "
                f"Disconnect (or reopen standalone) first, then load.")
            return
        self.session = loaded
        self._apply_session_to_ui()
        self._last_vol_path = path
        self._show_status(
            f"Loaded {loaded.point_count()} points from {path} — "
            f"acquire to append, select rows to delete, then "
            f"Write to File")

    def _apply_session_to_ui(self) -> None:
        """Push a (re)loaded session into every widget."""
        s = self.session
        self.balance_type_combo.blockSignals(True)
        self.balance_type_combo.setCurrentText(s.kind.value)
        self.balance_type_combo.blockSignals(False)
        self._active_kind = s.kind

        self.operator_edit.setText(s.operator)
        self.serial_edit.setText(s.serial_number)
        self.diameter_edit.setText(s.outer_diameter)
        self.date_edit.setText(s.cal_date.isoformat())

        self.max_table.setRowCount(6)
        for r, el in enumerate(s.elements):
            item = QTableWidgetItem(el.name)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.max_table.setItem(r, 0, item)
            ml = s.max_loads.get(el.name)
            self.max_table.setItem(
                r, 1, QTableWidgetItem("" if ml is None else f"{ml:g}"))
            if el.distance_tag:
                d = s.distances.get(el.distance_tag)
            elif el.name == "Mx":
                d = s.distances.get("roll_arm")
            else:
                d = None
            self.max_table.setItem(
                r, 2, QTableWidgetItem("" if d is None else f"{d:g}"))

        self.orient_combo.blockSignals(True)
        current = self.orient_combo.currentText()
        self.orient_combo.clear()
        self.orient_combo.addItems([o.key for o in s.orientations])
        # jump to the first orientation that has data (or keep current)
        first = next((o.key for o in s.orientations
                      if s.points.get(o.key)), current)
        if first:
            self.orient_combo.setCurrentText(first)
        self.orient_combo.blockSignals(False)
        self._preview_channels()
        self._on_orientation_changed()

    def _open_device_panel(self) -> None:
        """Open the driver's own app panel, sharing the live device so
        only one DAQ session ever exists."""
        if self.daq is None or not self.daq.connected:
            QMessageBox.information(
                self, "Not connected",
                "Connect the DAQ first — the device panel shares the "
                "calibration window's live connection.")
            return
        win = getattr(self, "_panel_win", None)
        if win is not None and win.isVisible():
            win.raise_()
            win.activateWindow()
            return
        backend = self.backend_combo.currentData()
        try:
            if backend == "ni6351":
                from ni_usb_6351.app.main_window import NiDaqPanel
                panel = NiDaqPanel(device=self.daq.driver, embedded=True)
                title = "NI USB-6351 — device panel (shared connection)"
            else:
                from strainbook_616.app.main_window import StrainbookPanel
                panel = StrainbookPanel(device=self.daq.driver,
                                        embedded=True)
                title = "StrainBook/616 — device panel (shared connection)"
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.critical(self, "Device panel", str(exc))
            return
        win = QMainWindow(self)
        win.setWindowTitle(title)
        win.setCentralWidget(panel)
        win.resize(950, 650)
        win.setStyleSheet(theme.get_stylesheet())
        win.show()
        self._panel_win = win

    def _write_vol(self) -> None:
        self._sync_session_meta()
        if self.session.point_count() == 0:
            QMessageBox.warning(self, "No data",
                                "No test points acquired yet.")
            return
        problems = validate_session(self.session)
        if problems:
            if QMessageBox.question(
                    self, "Incomplete balance info",
                    "\n".join(problems)
                    + "\n\nWrite the .vol anyway?") \
                    != QMessageBox.StandardButton.Yes:
                return
        path, _f = QFileDialog.getSaveFileName(
            self, "Save calibration", "", "Voltage cal (*.vol)")
        if not path:
            return
        if not path.lower().endswith(".vol"):
            path += ".vol"
        try:
            write_vol(self.session, path)
        except UnicodeEncodeError as exc:
            QMessageBox.critical(
                self, "Write failed",
                f"A metadata field contains a non-ASCII character the "
                f".vol format cannot store: {exc}. The existing file "
                f"was not touched.")
            return
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.critical(self, "Write failed", str(exc))
            return
        self._last_vol_path = path
        self._show_status(f"Wrote {path}")

    def _compute_summary(self) -> None:
        self._sync_session_meta()
        if self.session.point_count() == 0:
            QMessageBox.warning(self, "No data",
                                "No test points acquired yet.")
            return
        cal_type = self.cal_type_combo.currentText()
        try:
            s = summarize(self.session, cal_type)
            self._diag = diagnose(self.session, cal_type)
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.critical(self, "Reduction failed", str(exc))
            return
        text = report_text(self.session, s) + "\n" \
            + diagnostics_text(self._diag)
        self.summary_text.setPlainText(text)

        self._diag_sel = None
        sec = self.diag_section_combo
        sec.blockSignals(True)
        current_sec = sec.currentText()
        sec.clear()
        sec.addItems([o.key for o in self.session.orientations
                      if self.session.active_points(o.key)])
        if current_sec:
            sec.setCurrentText(current_sec)
        sec.blockSignals(False)
        combo = self.diag_element_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(self._diag.channels)
        # jump to the element with the most outliers (if any)
        counts = [sum(1 for p in self._diag.outliers()
                      if p.element == i)
                  for i in range(len(self._diag.channels))]
        if any(counts):
            combo.setCurrentIndex(int(np.argmax(counts)))
        combo.blockSignals(False)
        self._refresh_diag_plot()
        n_out = len(self._diag.outliers())
        if n_out:
            self._show_status(
                f"{n_out} outlier(s) flagged (|robust z| > "
                f"{OUTLIER_Z:g}) — click them in the plot to exclude "
                f"or delete, then recompute")

    # ── interactive fit diagnostics ──────────────────────────────────────
    def _diag_view_changed(self, *_a) -> None:
        channel_view = self.diag_view_combo.currentIndex() == 2
        self.diag_section_combo.setVisible(channel_view)
        self._refresh_diag_plot()

    def _refresh_diag_plot(self) -> None:
        diag = getattr(self, "_diag", None)
        self.diag_plot.clear()
        if diag is None:
            return
        col = self.diag_element_combo.currentIndex()
        if col < 0:
            return
        if self.diag_view_combo.currentIndex() == 2:
            self._refresh_channel_plot(col)
            return
        residual_view = self.diag_view_combo.currentIndex() == 1
        pts = [p for p in diag.points if p.element == col]
        if not pts:
            return
        loads = np.array([p.load for p in pts])
        ys = np.array([p.residual if residual_view else p.predicted
                       for p in pts])
        el = diag.channels[col]
        self.diag_plot.setLabel("bottom", f"Applied {el} load")
        self.diag_plot.setLabel(
            "left", f"{'Residual' if residual_view else 'Predicted'} "
                    f"{el}")
        r2 = diag.r_squared[col]
        r2c = diag.r_squared_clean[col]
        extra = (f"   (without outliers: {r2c:.6f})"
                 if abs(r2c - r2) > 1e-9 else "")
        self.diag_plot.setTitle(f"{el}: R² = {r2:.6f}{extra}")

        # reference line: y = x (prediction) or y = 0 (residuals)
        lo, hi = float(loads.min()), float(loads.max())
        ref = pg.PlotDataItem([lo, hi],
                              [0, 0] if residual_view else [lo, hi],
                              pen=pg.mkPen(theme.TEXT_DIM, width=1,
                                           style=Qt.PenStyle.DashLine))
        self.diag_plot.addItem(ref)

        def scatter(sel, brush, symbol_pen, size, name):
            data = [p for p in pts if sel(p)]
            if not data:
                return None
            item = pg.ScatterPlotItem(
                x=[p.load for p in data],
                y=[(p.residual if residual_view else p.predicted)
                   for p in data],
                data=data, size=size, brush=pg.mkBrush(brush),
                pen=symbol_pen, symbol="o", name=name)
            item.sigClicked.connect(self._diag_point_clicked)
            self.diag_plot.addItem(item)
            return item

        scatter(lambda p: not p.is_outlier, theme.ACCENT_LIGHT,
                None, 8, "points")
        scatter(lambda p: p.is_outlier, theme.WARNING,
                pg.mkPen(theme.ERROR, width=2), 11,
                f"outliers (|z| > {OUTLIER_Z:g})")
        sel = getattr(self, "_diag_sel", None)
        if sel is not None and sel.element == col:
            ring = pg.ScatterPlotItem(
                x=[sel.load],
                y=[sel.residual if residual_view else sel.predicted],
                size=17, brush=None,
                pen=pg.mkPen(theme.SUCCESS, width=2), symbol="o")
            self.diag_plot.addItem(ring)

    def _refresh_channel_plot(self, col: int) -> None:
        """Channel volts vs load within one section — the off-diagonal
        inspector. Values off the robust trend are the repairable
        cross-talk corruptions."""
        key = self.diag_section_combo.currentText()
        if not key or not self.session.active_points(key):
            return
        chan_name = [el.channel for el in self.session.elements][col]
        loads, volts, trend, z = channel_trend(self.session, key, col)
        idx_map = [i for i, p in enumerate(self.session.points[key])
                   if not p.excluded]
        self.diag_plot.setLabel("bottom", f"Applied load in [{key}]")
        self.diag_plot.setLabel("left", f"{chan_name} [mV]")
        span = float(np.ptp(volts)) * 1e3
        self.diag_plot.setTitle(
            f"[{key}] {chan_name} volts — span {span:.3f} mV")
        order = np.argsort(loads)
        self.diag_plot.addItem(pg.PlotDataItem(
            loads[order], trend[order] * 1e3,
            pen=pg.mkPen(theme.TEXT_DIM, width=1,
                         style=Qt.PenStyle.DashLine)))

        flagged = {(a.index) for a in self._diag.channel_anomalies
                   if a.key == key and a.channel == col}
        sels = []
        for r in range(len(volts)):
            sels.append(_ChannelSel(
                key=key, index=idx_map[r], channel=col,
                channel_name=chan_name, load=float(loads[r]),
                measured=float(volts[r]), expected=float(trend[r]),
                z=float(z[r]), flagged=idx_map[r] in flagged))

        def scatter(pred, brush, pen, size, name):
            data = [s for s in sels if pred(s)]
            if not data:
                return
            item = pg.ScatterPlotItem(
                x=[s.load for s in data],
                y=[s.measured * 1e3 for s in data],
                data=data, size=size, brush=pg.mkBrush(brush),
                pen=pen, symbol="o", name=name)
            item.sigClicked.connect(self._diag_point_clicked)
            self.diag_plot.addItem(item)

        scatter(lambda s: not s.flagged, theme.ACCENT_LIGHT, None, 8,
                "values")
        scatter(lambda s: s.flagged, theme.WARNING,
                pg.mkPen(theme.ERROR, width=2), 11, "anomalies")
        sel = getattr(self, "_diag_sel", None)
        if isinstance(sel, _ChannelSel) and sel.key == key \
                and sel.channel == col:
            self.diag_plot.addItem(pg.ScatterPlotItem(
                x=[sel.load], y=[sel.measured * 1e3], size=17,
                brush=None, pen=pg.mkPen(theme.SUCCESS, width=2),
                symbol="o"))

    def _diag_point_clicked(self, _item, points) -> None:
        if not len(points):
            return
        pd = points[0].data()
        self._diag_sel = pd
        if isinstance(pd, _ChannelSel):
            dev = pd.measured - pd.expected
            self.diag_detail.setText(
                f"[{pd.key}] row {pd.index + 1} {pd.channel_name}: "
                f"measured {pd.measured:+.6f} V, trend "
                f"{pd.expected:+.6f} V (dev {dev:+.2e}, "
                f"z = {pd.z:+.1f})"
                + ("   ← ANOMALY" if pd.flagged else ""))
            for b in (self.diag_exclude_btn, self.diag_delete_btn,
                      self.diag_goto_btn, self.diag_repair_btn,
                      self.diag_edit_btn):
                b.setEnabled(True)
            self._refresh_diag_plot()
            return
        self.diag_repair_btn.setEnabled(False)
        self.diag_edit_btn.setEnabled(False)
        p = self.session.points[pd.key][pd.index]
        stds = ""
        if p.stds:
            stds = f"   std {max(p.stds):.2e} V"
        self.diag_detail.setText(
            f"[{pd.key}] row {pd.index + 1}:  applied {pd.load:g}, "
            f"predicted {pd.predicted:.4f}, residual "
            f"{pd.residual:+.4f} (z = {pd.zscore:+.1f})   "
            f"excitation {pd.excitation:.3f} V{stds}"
            + ("   ← OUTLIER" if pd.is_outlier else ""))
        for b in (self.diag_exclude_btn, self.diag_delete_btn,
                  self.diag_goto_btn):
            b.setEnabled(True)
        self._refresh_diag_plot()

    def _diag_exclude(self) -> None:
        sel = getattr(self, "_diag_sel", None)
        if sel is None:
            return
        self.session.points[sel.key][sel.index].excluded = True
        self._diag_sel = None
        self._show_status(f"Excluded [{sel.key}] row {sel.index + 1} — "
                          f"refitting (excluded points are also left "
                          f"out of the written .vol)")
        self._after_diag_edit()

    def _diag_include_all(self) -> None:
        n = self.session.excluded_count()
        if not n:
            return
        for pts in self.session.points.values():
            for p in pts:
                p.excluded = False
        self._show_status(f"Re-included {n} point(s) — refitting")
        self._after_diag_edit()

    def _diag_delete(self) -> None:
        sel = getattr(self, "_diag_sel", None)
        if sel is None:
            return
        if QMessageBox.question(
                self, "Delete point",
                f"Permanently delete [{sel.key}] row {sel.index + 1} "
                f"(applied {sel.load:g})? Exclude is reversible; "
                f"delete is not.") != QMessageBox.StandardButton.Yes:
            return
        self.session.remove_point(sel.key, sel.index)
        self._diag_sel = None
        self._after_diag_edit()

    def _diag_repair_value(self) -> None:
        sel = getattr(self, "_diag_sel", None)
        if not isinstance(sel, _ChannelSel):
            return
        p = self.session.points[sel.key][sel.index]
        p.volts = list(p.volts)
        p.volts[sel.channel] = sel.expected
        self._show_status(
            f"Repaired [{sel.key}] row {sel.index + 1} "
            f"{sel.channel_name}: {sel.measured:+.6f} → "
            f"{sel.expected:+.6f} V (section trend) — refitting")
        self._after_diag_edit()

    def _diag_edit_value(self) -> None:
        sel = getattr(self, "_diag_sel", None)
        if not isinstance(sel, _ChannelSel):
            return
        text, ok = QInputDialog.getText(
            self, "Edit channel value",
            f"[{sel.key}] row {sel.index + 1} {sel.channel_name} "
            f"volts\n(measured {sel.measured:+.6f}, trend "
            f"{sel.expected:+.6f}):",
            text=f"{sel.measured:.6f}")
        if not ok:
            return
        try:
            value = float(text)
        except ValueError:
            QMessageBox.warning(self, "Edit value",
                                f"Not a number: {text!r}")
            return
        p = self.session.points[sel.key][sel.index]
        p.volts = list(p.volts)
        p.volts[sel.channel] = value
        self._show_status(
            f"Edited [{sel.key}] row {sel.index + 1} "
            f"{sel.channel_name}: {sel.measured:+.6f} → {value:+.6f} V "
            f"— refitting")
        self._after_diag_edit()

    def _diag_goto(self) -> None:
        sel = getattr(self, "_diag_sel", None)
        if sel is None:
            return
        self.tabs.setCurrentIndex(1)
        self.orient_combo.setCurrentText(sel.key)
        self._refresh_mtable()      # no-op signal if already selected
        self.mtable.selectRow(sel.index)

    def _after_diag_edit(self) -> None:
        for b in (self.diag_exclude_btn, self.diag_delete_btn,
                  self.diag_goto_btn, self.diag_repair_btn,
                  self.diag_edit_btn):
            b.setEnabled(False)
        self._refresh_mtable()
        if self.session.point_count() > 0:
            self._compute_summary()

    def _summarize_existing(self) -> None:
        path, _f = QFileDialog.getOpenFileName(
            self, "Open .vol", "", "Voltage cal (*.vol)")
        if not path:
            return
        from ..daq import _ensure_devices_path
        _ensure_devices_path()
        from ni_usb_6351 import balcal
        try:
            cal = balcal.read_vol_file(path)
            cal = balcal.calc_coeffs(cal,
                                     self.cal_type_combo.currentText())
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.critical(self, "Read failed", str(exc))
            return
        lines = [f"File: {path}", balcal.balance_summary(cal), "",
                 f"{'Element':<12s}{'R^2':>10s}{'bias':>12s}"]
        for i, name in enumerate(cal.force_channels):
            lines.append(f"{name:<12s}{cal.r_squared[i]:10.6f}"
                         f"{cal.bias[i]:12.4g}")
        self.summary_text.setPlainText("\n".join(lines))
        self.tabs.setCurrentIndex(3)

    def _save_report(self) -> None:
        text = self.summary_text.toPlainText()
        if not text.strip():
            self._compute_summary()
            text = self.summary_text.toPlainText()
            if not text.strip():
                return
        path, _f = QFileDialog.getSaveFileName(
            self, "Save report", "", "Text report (*.txt)")
        if not path:
            return
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        self._show_status(f"Report saved to {path}")

    # ── misc ─────────────────────────────────────────────────────────────
    def _show_status(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 10_000)

    def closeEvent(self, event) -> None:              # noqa: N802
        self._live_timer.stop()
        panel = getattr(self, "_panel_win", None)
        if panel is not None:
            panel.close()
        if self._acquire_thread is not None:
            if self.daq is not None:
                self.daq.abort_acquire()
            self._acquire_thread.wait(5000)
        if self.daq is not None and self._external_device is None:
            self.daq.disconnect()
        super().closeEvent(event)
