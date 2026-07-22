"""Main window + coordinator panel for the LSWT Sting app.

Layout: connection bar (COM port, sim, lamp, FAULT, STOP ALL) → Motion
tab (Alpha | Beta axis boxes with absolute Go / step jog / zeroing, plus
Go Both and the fault controls) → History tab (angle vs time) → Limits
tab (soft travel limits, park + comms behaviour).
"""

from __future__ import annotations

import logging
from typing import Optional

import pyqtgraph as pg
from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QMainWindow,
    QMessageBox, QPushButton, QSpinBox, QStatusBar, QTabWidget,
    QVBoxLayout, QWidget,
)

from lswt_sting import theme
from lswt_sting.config import StingConfig
from lswt_sting.device import StingDrive
from lswt_sting.protocol import StingError

from .settings_dialog import SettingsDialog

log = logging.getLogger(__name__)

theme.apply_pyqtgraph_theme()

REINIT_WARNING = (
    "Reinitialize re-runs the drive bring-up sequence, including the Z "
    "drive reset.\n\nThe legacy tool warns the reset MAY CAUSE "
    "UNCONTROLLED MOVEMENT if the sting is not in a safe position, and "
    "both axes must be re-zeroed afterwards.\n\nIs the sting in a safe "
    "position?")


class _AxisBox(QGroupBox):
    """Readout + motion controls for one sting axis.

    Un-zeroed axes show a "not zeroed" hint and only allow step jogs;
    the absolute target/Go unlocks once the operator zeroes the axis
    ("Set Current Angle…" → ``PZ``).
    """

    def __init__(self, name: str, color: str, parent=None):
        super().__init__(f"{name} axis", parent)
        self.axis_name = name
        self._zeroed = False
        g = QGridLayout(self)

        chip = QLabel()
        chip.setFixedSize(10, 10)
        chip.setStyleSheet(f"background-color: {color}; "
                           f"border-radius: 5px;")
        g.addWidget(chip, 0, 0)
        self.big_lbl = QLabel("--")
        self.big_lbl.setStyleSheet(
            "font-family: 'Segoe UI'; font-size: 26pt; font-weight: 600; "
            f"color: {theme.TEXT};")
        g.addWidget(self.big_lbl, 0, 1, 1, 2)
        self.unit_lbl = QLabel("deg")
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
        self.go_btn = QPushButton("Go")
        self.go_btn.setObjectName("primary")
        g.addWidget(self.go_btn, 2, 2)
        self.stop_btn = QPushButton("Stop")
        g.addWidget(self.stop_btn, 2, 3)

        # step-jog row (the legacy "Degrees per Step" buttons)
        self.step_minus = QPushButton("Step −")
        self.step_plus = QPushButton("Step +")
        for btn in (self.step_minus, self.step_plus):
            btn.setMinimumHeight(40)
            btn.setStyleSheet("font-size: 12pt; font-weight: bold;")
        g.addWidget(self.step_minus, 3, 0, 1, 1)
        self.step_size = QDoubleSpinBox()
        self.step_size.setDecimals(3)
        self.step_size.setRange(0.001, 10.0)
        self.step_size.setSingleStep(0.1)
        self.step_size.setValue(0.5)
        self.step_size.setSuffix("°/step")
        self.step_size.setToolTip("Degrees moved per Step +/− click")
        g.addWidget(self.step_size, 3, 1, 1, 2)
        g.addWidget(self.step_plus, 3, 3, 1, 1)

        self.zero_btn = QPushButton("Set Current Angle…")
        self.zero_btn.setToolTip(
            "Declare the sting's physical angle and zero the step "
            "counter (PZ) — required before absolute moves")
        g.addWidget(self.zero_btn, 4, 0, 1, 4)

    def set_state(self, angle: float, counts: int, moving: bool,
                  target, zeroed: bool):
        self._zeroed = zeroed
        if zeroed:
            self.big_lbl.setText(f"{angle:+8.3f}")
            self.sub_lbl.setText(f"{counts:+d} steps")
        else:
            self.big_lbl.setText("— not zeroed —")
            self.sub_lbl.setText(f"{counts:+d} steps — jog only")
        if moving:
            if target is not None:
                self.state_lbl.setText(f"→ {target:+.2f}°")
            else:
                self.state_lbl.setText("MOVING")
            self.state_lbl.setStyleSheet(f"color: {theme.WARNING};")
        else:
            self.state_lbl.setText("idle")
            self.state_lbl.setStyleSheet(f"color: {theme.TEXT_DIM};")

    def set_enables(self, connected: bool, faulted: bool):
        motion = connected and not faulted
        absolute = motion and self._zeroed
        self.go_btn.setEnabled(absolute)
        self.target.setEnabled(absolute)
        tip = ("" if self._zeroed else
               "Zero the axis (Set Current Angle…) to enable absolute "
               "moves")
        self.go_btn.setToolTip(tip)
        self.target.setToolTip(tip)
        for w in (self.step_minus, self.step_plus, self.step_size,
                  self.stop_btn, self.zero_btn):
            w.setEnabled(motion)

    def set_limits(self, lo: float, hi: float):
        self.target.setRange(lo, hi)


class StingPanel(QWidget):
    """The complete LSWT sting GUI (also embeddable in host suites).

    ``device``/``embedded`` support hosting the EXACT same panel inside a
    larger suite: pass the host's live :class:`StingDrive` so only ONE
    serial connection ever exists, and ``embedded=True`` to hide the
    Connection row (the host owns connect/disconnect). With the defaults
    the standalone app behaviour is unchanged — the panel builds and owns
    its own drive.
    """

    statusSignal = pyqtSignal(str)
    searchDone = pyqtSignal(list)       # [comscan.ProbeResult]

    def __init__(self, cfg: Optional[StingConfig] = None, parent=None,
                 *, device: Optional[StingDrive] = None,
                 embedded: bool = False):
        super().__init__(parent)
        self.setObjectName("root")
        self._embedded = bool(embedded)
        if device is not None:
            self.device = device
            self.config = cfg if cfg is not None else device.config
        else:
            self.config = cfg or StingConfig()
            self.device = StingDrive(self.config)

        self._build_ui()
        if not self._embedded:
            # standalone: pipe driver status (poll thread!) through the
            # signal so it lands on the GUI thread. Embedded hosts keep
            # their own on_status wiring.
            self.device.on_status = self.statusSignal.emit

        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(100)
        self._ui_timer.timeout.connect(self._refresh_ui)
        self._ui_timer.start()

        self._last_connected = self.device.connected
        self._last_fault = self.device.fault
        if self._last_connected:            # embedded, already-live host
            self._apply_limits()
        self._set_connected_ui(self._last_connected)

    # ── UI build ─────────────────────────────────────────────────────────
    def _build_ui(self):
        self.setStyleSheet(theme.get_stylesheet())
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

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
            "Probe every COM port with a read-only '1R' status query "
            "and select the one where the sting chain answers")
        self.search_btn.clicked.connect(self._handle_search)
        self.searchDone.connect(self._search_finished)
        cl.addWidget(self.search_btn)
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
        self.fault_lbl = QLabel("FAULT")
        self.fault_lbl.setProperty("mono", "true")
        cl.addWidget(self.fault_lbl)
        self._show_fault(None)
        self.stop_all_btn = QPushButton("STOP ALL")
        self.stop_all_btn.setObjectName("danger")
        self.stop_all_btn.setMinimumHeight(48)
        self.stop_all_btn.setMinimumWidth(140)
        self.stop_all_btn.setToolTip(
            "Immediate stop of both axes — no confirmation")
        self.stop_all_btn.clicked.connect(self.device.stop_all)
        cl.addWidget(self.stop_all_btn)
        root.addWidget(conn)
        if self._embedded:              # host owns connect/disconnect
            conn.hide()

        self.tabs = QTabWidget()
        self.tabs.addTab(self._build_motion_tab(), "Motion")
        self.tabs.addTab(self._build_history_tab(), "History")
        self.tabs.addTab(self._build_limits_tab(), "Limits")
        root.addWidget(self.tabs, 1)

    def _com_ports(self) -> list:
        try:
            from serial.tools import list_ports
            ports = [p.device for p in list_ports.comports()]
        except Exception:                              # noqa: BLE001
            ports = []
        for extra in ([self.config.com_port] +
                      [f"COM{i}" for i in range(1, 9)]):
            if extra not in ports:
                ports.append(extra)
        return ports

    def _build_motion_tab(self) -> QWidget:
        motion = QWidget()
        ml = QVBoxLayout(motion)
        ml.setSpacing(8)

        boxes = QHBoxLayout()
        self.alpha_box = _AxisBox("Alpha", theme.series_color(0))
        self.beta_box = _AxisBox("Beta", theme.series_color(1))
        for box in (self.alpha_box, self.beta_box):
            name = box.axis_name
            box.go_btn.clicked.connect(
                lambda _=False, b=box: self._go_single(b))
            box.stop_btn.clicked.connect(
                lambda _=False, n=name: self.device.stop_axis(n))
            box.step_plus.clicked.connect(
                lambda _=False, b=box: self._step(b, +1))
            box.step_minus.clicked.connect(
                lambda _=False, b=box: self._step(b, -1))
            box.zero_btn.clicked.connect(
                lambda _=False, n=name: self._ask_zero(n))
            boxes.addWidget(box, 2)

        side = QVBoxLayout()
        both = QGroupBox("Go Both")
        bg = QGridLayout(both)
        bg.addWidget(QLabel("α"), 0, 0)
        self.both_alpha = QDoubleSpinBox()
        self.both_alpha.setDecimals(2)
        self.both_alpha.setSuffix("°")
        bg.addWidget(self.both_alpha, 0, 1)
        bg.addWidget(QLabel("β"), 1, 0)
        self.both_beta = QDoubleSpinBox()
        self.both_beta.setDecimals(2)
        self.both_beta.setSuffix("°")
        bg.addWidget(self.both_beta, 1, 1)
        self.both_btn = QPushButton("Go Both")
        self.both_btn.setObjectName("success")
        self.both_btn.clicked.connect(
            lambda: self._move(alpha=self.both_alpha.value(),
                               beta=self.both_beta.value()))
        bg.addWidget(self.both_btn, 0, 2, 2, 1)
        side.addWidget(both)

        drives = QGroupBox("Drive")
        dg = QVBoxLayout(drives)
        self.reset_fault_btn = QPushButton("Reset Fault")
        self.reset_fault_btn.setToolTip(
            "Clear the latched fault after the cause has been addressed")
        self.reset_fault_btn.clicked.connect(self._handle_reset_fault)
        dg.addWidget(self.reset_fault_btn)
        self.reinit_btn = QPushButton("Reinitialize Drives")
        self.reinit_btn.setToolTip(
            "Re-run the drive init sequence (includes the Z reset — "
            "may cause uncontrolled movement)")
        self.reinit_btn.clicked.connect(self._handle_reinit)
        dg.addWidget(self.reinit_btn)
        side.addWidget(drives)
        side.addStretch(1)
        boxes.addLayout(side, 1)
        ml.addLayout(boxes)
        ml.addStretch(1)
        return motion

    def _build_history_tab(self) -> QWidget:
        hist = QWidget()
        hl = QVBoxLayout(hist)
        hl.setSpacing(8)

        ind = QHBoxLayout()
        self.moving_lbl = QLabel("idle")
        self.moving_lbl.setProperty("mono", "true")
        ind.addWidget(QLabel("Motion:"))
        ind.addWidget(self.moving_lbl)
        ind.addStretch(1)
        hl.addLayout(ind)

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
                             pen=pg.mkPen(theme.series_color(0), width=1)),
            "Beta": pi.plot([], [], name="Beta",
                            pen=pg.mkPen(theme.series_color(1), width=1)),
        }
        hl.addWidget(self.plot, 1)
        return hist

    def _build_limits_tab(self) -> QWidget:
        lim = QWidget()
        ll = QVBoxLayout(lim)
        ll.setSpacing(8)

        travel = QGroupBox("Soft travel limits (deg)")
        tf = QFormLayout(travel)
        self.a_min, self.a_max = self._limit_pair(self.config.alpha)
        tf.addRow("Alpha min / max", self._row(self.a_min, self.a_max))
        self.b_min, self.b_max = self._limit_pair(self.config.beta)
        tf.addRow("Beta min / max", self._row(self.b_min, self.b_max))
        note = QLabel("Limit edits take effect immediately; comms "
                      "settings apply at the next connect.")
        note.setObjectName("dim")
        tf.addRow(note)
        ll.addWidget(travel)

        beh = QGroupBox("Behaviour")
        bf = QFormLayout(beh)
        self.park_chk = QCheckBox("Park Alpha on disconnect")
        self.park_chk.setChecked(self.config.park_on_disconnect)
        self.park_chk.toggled.connect(self._limits_changed)
        bf.addRow(self.park_chk)
        self.park_deg = QDoubleSpinBox()
        self.park_deg.setRange(-360.0, 360.0)
        self.park_deg.setDecimals(1)
        self.park_deg.setValue(self.config.park_alpha_deg)
        self.park_deg.setSuffix("°")
        self.park_deg.valueChanged.connect(self._limits_changed)
        bf.addRow("Park Alpha at", self.park_deg)
        ll.addWidget(beh)

        comms = QGroupBox("Comms (next connect)")
        cf = QFormLayout(comms)
        self.init_reset_chk = QCheckBox("Send Z (drive reset) at connect")
        self.init_reset_chk.setChecked(self.config.init_reset)
        self.init_reset_chk.toggled.connect(self._limits_changed)
        cf.addRow(self.init_reset_chk)
        self.poll_ms = QSpinBox()
        self.poll_ms.setRange(20, 2000)
        self.poll_ms.setValue(self.config.poll_ms)
        self.poll_ms.setSuffix(" ms")
        self.poll_ms.valueChanged.connect(self._limits_changed)
        cf.addRow("Poll period", self.poll_ms)
        ll.addWidget(comms)
        ll.addStretch(1)
        return lim

    @staticmethod
    def _limit_pair(ax_cfg):
        lo = QDoubleSpinBox()
        lo.setRange(-360.0, 360.0)
        lo.setDecimals(1)
        lo.setValue(ax_cfg.min_deg)
        lo.setSuffix("°")
        hi = QDoubleSpinBox()
        hi.setRange(-360.0, 360.0)
        hi.setDecimals(1)
        hi.setValue(ax_cfg.max_deg)
        hi.setSuffix("°")
        return lo, hi

    def _row(self, *widgets):
        row = QHBoxLayout()
        for w in widgets:
            row.addWidget(w)
            if isinstance(w, QDoubleSpinBox):
                w.valueChanged.connect(self._limits_changed)
        return row

    # ── actions ──────────────────────────────────────────────────────────
    def _handle_search(self):
        """Probe all COM ports for the sting chain (worker thread —
        each silent port costs the read timeout)."""
        if self.device.connected:
            self.statusSignal.emit(
                "Disconnect before searching — the open port would "
                "answer the probe")
            return
        self.search_btn.setEnabled(False)
        self.search_btn.setText("Searching…")
        self.statusSignal.emit("Probing COM ports with '1R'…")

        def run():
            from lswt_sting import comscan
            try:
                results = comscan.search()
            except Exception as exc:               # noqa: BLE001
                log.exception("COM search failed")
                results = []
                self.statusSignal.emit(f"COM search failed: {exc}")
            self.searchDone.emit(results)

        import threading
        threading.Thread(target=run, name="sting-comscan",
                         daemon=True).start()

    def _search_finished(self, results):
        self.search_btn.setEnabled(True)
        self.search_btn.setText("Search…")
        current = self.port.currentText()
        self.port.clear()
        hits = [r for r in results if r.is_sting]
        for r in results:
            self.port.addItem(r.port.device)
            idx = self.port.count() - 1
            self.port.setItemData(idx, r.summary,
                                  Qt.ItemDataRole.ToolTipRole)
        if not results:
            self.port.addItems(self._com_ports())
        if hits:
            self.port.setCurrentText(hits[0].port.device)
            self.statusSignal.emit(
                f"Sting chain found on {hits[0].port.device} "
                f"({hits[0].port.description or 'no description'}) — "
                f"ready to Connect")
        else:
            if current:
                self.port.setCurrentText(current)
            detail = "; ".join(r.summary for r in results) or "no ports"
            self.statusSignal.emit(
                f"No sting chain found — {detail}")
            log.info("COM search results: %s",
                     [r.summary for r in results])

    def _handle_connect(self):
        self.config.com_port = (self.port.currentText().strip() or
                                self.config.com_port)
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
        except (ValueError, RuntimeError, StingError) as exc:
            self.statusSignal.emit(str(exc))

    def _go_single(self, box: _AxisBox):
        target = box.target.value()
        if box.axis_name == "Alpha":
            self._move(alpha=target)
        else:
            self._move(beta=target)

    def _step(self, box: _AxisBox, sign: int):
        try:
            self.device.move_by(box.axis_name,
                                sign * box.step_size.value())
        except (ValueError, RuntimeError, StingError) as exc:
            self.statusSignal.emit(str(exc))

    def _ask_zero(self, name: str):
        st = self.device.state()[name]
        angle, ok = QInputDialog.getDouble(
            self, f"Set {name} current angle",
            f"Physical {name} angle right now (deg):",
            st["angle"], -360.0, 360.0, 3)
        if not ok:
            return
        reply = QMessageBox.question(
            self, "Confirm zero",
            f"Declare the sting's physical {name} angle as "
            f"{angle:+.3f}° and zero the step counter (PZ)?\n\n"
            f"All future absolute moves reference this position.")
        if reply != QMessageBox.StandardButton.Yes:
            return
        self._zero_axis(name, angle)

    def _zero_axis(self, name: str, angle: float):
        """Zero one axis at the declared physical angle (dialog + tests)."""
        try:
            self.device.set_current_angle(name, angle)
        except (ValueError, RuntimeError, StingError) as exc:
            self.statusSignal.emit(str(exc))
            return
        self._refresh_ui()

    def _handle_reset_fault(self):
        try:
            self.device.reset_fault()
        except (ValueError, RuntimeError, StingError) as exc:
            self.statusSignal.emit(str(exc))

    def _handle_reinit(self):
        reply = QMessageBox.warning(
            self, "Reinitialize drives", REINIT_WARNING,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            self.device.reinitialize(confirm_safe=True)
        except (ValueError, RuntimeError, StingError) as exc:
            self.statusSignal.emit(f"Reinitialize failed: {exc}")

    def _limits_changed(self, *_):
        cfg = self.config
        cfg.alpha.min_deg = min(self.a_min.value(), self.a_max.value())
        cfg.alpha.max_deg = max(self.a_min.value(), self.a_max.value())
        cfg.beta.min_deg = min(self.b_min.value(), self.b_max.value())
        cfg.beta.max_deg = max(self.b_min.value(), self.b_max.value())
        cfg.park_on_disconnect = self.park_chk.isChecked()
        cfg.park_alpha_deg = self.park_deg.value()
        cfg.init_reset = self.init_reset_chk.isChecked()
        cfg.poll_ms = self.poll_ms.value()
        self.device.set_config(cfg)     # limits/zero take effect at once
        self._apply_limits()

    def _sync_limits_widgets(self):
        """Refresh the Limits-tab widgets from cfg (settings dialog or a
        loaded config may have changed the shared fields)."""
        cfg = self.config
        for w, value in ((self.a_min, cfg.alpha.min_deg),
                         (self.a_max, cfg.alpha.max_deg),
                         (self.b_min, cfg.beta.min_deg),
                         (self.b_max, cfg.beta.max_deg),
                         (self.park_deg, cfg.park_alpha_deg),
                         (self.poll_ms, cfg.poll_ms)):
            w.blockSignals(True)
            w.setValue(value)
            w.blockSignals(False)
        for w, value in ((self.park_chk, cfg.park_on_disconnect),
                         (self.init_reset_chk, cfg.init_reset)):
            w.blockSignals(True)
            w.setChecked(value)
            w.blockSignals(False)

    def _apply_limits(self):
        self.alpha_box.set_limits(self.config.alpha.min_deg,
                                  self.config.alpha.max_deg)
        self.beta_box.set_limits(self.config.beta.min_deg,
                                 self.config.beta.max_deg)
        self.both_alpha.setRange(self.config.alpha.min_deg,
                                 self.config.alpha.max_deg)
        self.both_beta.setRange(self.config.beta.min_deg,
                                self.config.beta.max_deg)

    # ── state → widgets ──────────────────────────────────────────────────
    def _set_connected_ui(self, connected: bool):
        self.connect_btn.setEnabled(not connected)
        self.disconnect_btn.setEnabled(connected)
        for w in (self.port, self.sim):
            w.setEnabled(not connected)
        # STOP ALL: always live while connected, no confirmation
        self.stop_all_btn.setEnabled(connected)
        self._update_motion_enables()
        if not connected:
            self._set_lamp("DISCONNECTED", theme.TEXT_DIM)
        elif self.device.sim_mode:
            self._set_lamp("SIMULATION", theme.WARNING)
        else:
            self._set_lamp("LIVE", theme.SUCCESS)

    def _set_lamp(self, text: str, color: str):
        self.lamp.setText(text)
        self.lamp.setStyleSheet(f"color: {color}; font-weight: bold;")

    def _show_fault(self, fault: Optional[str]):
        if fault:
            self.fault_lbl.setStyleSheet(
                f"color: white; background-color: {theme.ERROR}; "
                f"font-weight: bold; padding: 4px 10px; "
                f"border-radius: 4px;")
            self.fault_lbl.setToolTip(fault)
        else:
            self.fault_lbl.setStyleSheet(
                f"color: {theme.TEXT_DISABLED}; font-weight: bold; "
                f"padding: 4px 10px;")
            self.fault_lbl.setToolTip("no fault")

    def _update_motion_enables(self):
        connected = self.device.connected
        faulted = bool(self.device.fault)
        st = self.device.state()
        self.alpha_box.set_enables(connected, faulted)
        self.beta_box.set_enables(connected, faulted)
        both_ok = (connected and not faulted and
                   st["Alpha"]["zeroed"] and st["Beta"]["zeroed"])
        self.both_btn.setEnabled(both_ok)
        self.both_alpha.setEnabled(both_ok)
        self.both_beta.setEnabled(both_ok)
        self.reset_fault_btn.setEnabled(connected and faulted)
        self.reinit_btn.setEnabled(connected and not faulted)

    # ── refresh ──────────────────────────────────────────────────────────
    def _refresh_ui(self):
        # track connection changes made OUTSIDE the panel's own buttons
        # (embedded hosts, serial watchdog) so the controls come alive /
        # lock down without a Connect click.
        connected = self.device.connected
        if connected != self._last_connected:
            self._last_connected = connected
            if connected:
                self._apply_limits()
            self._set_connected_ui(connected)

        fault = self.device.fault
        if fault != self._last_fault:
            self._last_fault = fault
            self._show_fault(fault)
        if not connected:
            return

        state = self.device.state()
        a, b = state["Alpha"], state["Beta"]
        self.alpha_box.set_state(a["angle"], a["counts"], a["moving"],
                                 a["target"], a["zeroed"])
        self.beta_box.set_state(b["angle"], b["counts"], b["moving"],
                                b["target"], b["zeroed"])
        self._update_motion_enables()

        moving = [n for n in ("Alpha", "Beta") if state[n]["moving"]]
        if moving:
            self.moving_lbl.setText("MOVING: " + " + ".join(moving))
            self.moving_lbl.setStyleSheet(f"color: {theme.WARNING}; "
                                          f"font-weight: bold;")
        else:
            self.moving_lbl.setText("idle")
            self.moving_lbl.setStyleSheet(f"color: {theme.TEXT_DIM};")

        window = self.config.plot_window_s
        n = int(window / max(self.config.poll_ms / 1000.0, 0.02)
                * 1.1) + 2
        data = self.device.ring.tail(n)
        t = data["t"]
        if t.size >= 2 and self.plot.isVisible():
            x = t - t[-1]
            keep = x >= -window
            for name, curve in self._curves.items():
                curve.setData(x[keep], data[name][keep])
            self.plot.getPlotItem().setXRange(-window, 0.0, padding=0)

    def shutdown(self):
        try:
            self.device.disconnect()
        except Exception as exc:                       # noqa: BLE001
            log.warning("shutdown: %s", exc)

    def apply_settings(self):
        self._sync_limits_widgets()
        self.device.set_config(self.config)
        self._apply_limits()


class StingMainWindow(QMainWindow):
    def __init__(self, cfg: Optional[StingConfig] = None):
        super().__init__()
        self.setWindowTitle("LSWT Sting Drive")
        self.resize(1000, 720)
        self.setStyleSheet(theme.get_stylesheet())
        self.panel = StingPanel(cfg, self)
        self.setCentralWidget(self.panel)

        sb = QStatusBar()
        self.setStatusBar(sb)
        self._sb_status = QLabel("Idle")
        self._sb_status.setProperty("mono", "true")
        sb.addPermanentWidget(self._sb_status, 1)
        self.panel.statusSignal.connect(self._sb_status.setText)

        self._build_menus()

    def closeEvent(self, event):                       # noqa: N802
        """No brake on the sting: closing the window while connected
        parks Alpha (if configured) and checkpoints the position via
        the normal disconnect path before the app dies."""
        dev = self.panel.device
        if dev.connected:
            self._sb_status.setText("Parking && disconnecting…")
            try:
                dev.disconnect()
            except Exception as exc:                   # noqa: BLE001
                log.exception("disconnect on close failed")
                self._sb_status.setText(f"Disconnect failed: {exc}")
        super().closeEvent(event)

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
                                              "sting_config.json",
                                              "JSON (*.json)")
        if path:
            self.panel.config.save(path)

    def _load_cfg(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load config", "",
                                              "JSON (*.json)")
        if path:
            self.apply_config(StingConfig.load(path))
            self.statusBar().showMessage(f"Loaded {path}", 3000)

    def apply_config(self, cfg: StingConfig):
        """Adopt a loaded config: rebind the drive and refresh all tabs."""
        panel = self.panel
        panel.config = cfg
        panel.port.setCurrentText(cfg.com_port)
        panel.sim.setChecked(cfg.force_sim)
        panel.apply_settings()          # syncs Limits tab + set_config
        panel._refresh_ui()

    def closeEvent(self, event):
        self.panel.shutdown()
        super().closeEvent(event)
