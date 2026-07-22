"""Main window + coordinator panel for the ATE balance app.

``AteBalancePanel`` owns the driver, the ring buffer, the dwell accumulator and
the aux source, and is the single place where IO-thread callbacks are marshalled
onto the GUI thread (status/reply via Qt signals; frames stashed and pulled by a
20 Hz timer, exactly as the wtdaq DAQbook app does).

``AteBalanceMainWindow`` is the standalone shell (status bar + menus).
"""

from __future__ import annotations

import logging
import time
from typing import Optional

from PyQt6.QtCore import QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QFileDialog, QLabel, QMainWindow, QStatusBar, QTabWidget, QVBoxLayout,
    QWidget,
)

from ate_balance import protocol as P
from ate_balance import theme
from ate_balance.aux_source import SimAuxSource
from ate_balance.config import AteConfig
from ate_balance.datamodel import RingBuffer
from ate_balance.device import AteBalanceDevice
from ate_balance.reduction import DwellAccumulator, build_master_frame

from .panels.connect_panel import ConnectPanel
from .panels.live_panel import LivePanel
from .panels.motion_panel import MotionPanel
from .panels.run_panel import RunPanel
from .settings_dialog import SettingsDialog

log = logging.getLogger(__name__)


class AteBalancePanel(QWidget):
    """The complete ATE balance GUI (also embeddable in host suites).

    ``device``/``embedded`` support hosting the EXACT same panel inside
    Freestream: pass the host's live :class:`AteBalanceDevice` so only ONE
    TMS client ever exists, and ``embedded=True`` to hide the Connection
    row (the host owns connect/disconnect). The device's single-slot
    ``on_frame``/``on_reply``/``on_status`` callbacks are CHAINED, not
    stolen — the host adapter's own hooks keep firing — and ``detach()``
    restores them when the hosting dialog closes. With the defaults the
    standalone app behaviour is unchanged — the panel builds and owns its
    own device.
    """

    statusSignal = pyqtSignal(str)
    replySignal = pyqtSignal(object)
    pointSignal = pyqtSignal(object)

    def __init__(self, cfg: Optional[AteConfig] = None, aux=None, parent=None,
                 *, device: Optional[AteBalanceDevice] = None,
                 embedded: bool = False):
        super().__init__(parent)
        self.setObjectName("root")

        self._embedded = bool(embedded)
        if device is not None:
            self.config = cfg if cfg is not None else device.config
            self.device = device
        else:
            self.config = cfg or AteConfig()
            self.device = AteBalanceDevice(self.config)
        self.ring = RingBuffer()
        self.dwell = DwellAccumulator()
        # aux: anything with dynamic_pressure()/temperature_k()/close();
        # a real DaqBook via daqbook_2000.DaqbookAuxSource, else the sim.
        self._external_aux = aux is not None
        self.aux = aux if aux is not None else SimAuxSource(q_pa=500.0)

        # live attitude / tunnel-q state (read by the IO thread)
        self._alpha = 0.0
        self._beta = 0.0
        self._q = 500.0
        self._latest = None

        # rate tracking
        self._last_count = 0
        self._last_time = 0.0
        self._rate = 0.0

        self._build_ui()

        # device callbacks -> marshal to GUI thread. The device has single
        # -slot callbacks; when embedded the host adapter already claimed
        # them, so CHAIN (host first, then this panel) and let detach()
        # hand them back on dialog close.
        self._prev_callbacks = (self.device.on_status, self.device.on_reply,
                                self.device.on_frame)
        if self._embedded:
            self.device.on_status = self._chain(self._prev_callbacks[0],
                                                self.statusSignal.emit)
            self.device.on_reply = self._chain(self._prev_callbacks[1],
                                               self.replySignal.emit)
            self.device.on_frame = self._chain(self._prev_callbacks[2],
                                               self._on_frame)
        else:
            self.device.on_status = self.statusSignal.emit
            self.device.on_reply = self.replySignal.emit
            self.device.on_frame = self._on_frame      # IO thread, stash only
        self.replySignal.connect(self._handle_reply)

        # timers
        self._ui_timer = QTimer(self)
        self._ui_timer.setInterval(50)                 # 20 Hz
        self._ui_timer.timeout.connect(self._refresh_ui)
        self._ui_timer.start()

        self._slow_timer = QTimer(self)
        self._slow_timer.setInterval(500)
        self._slow_timer.timeout.connect(self._slow_tick)
        self._slow_timer.start()

        self.connect_panel.set_state(self.device.connected,
                                     self.device.sim_mode,
                                     self.device.link_up)

    @staticmethod
    def _chain(host_cb, panel_cb):
        """Fan a single-slot device callback out to host THEN panel."""
        if host_cb is None:
            return panel_cb

        def fan(*args):
            host_cb(*args)
            panel_cb(*args)
        return fan

    def detach(self):
        """Restore the device callbacks captured at construction (embedded
        hosts call this when the containing dialog closes, so the adapter's
        hooks survive and nothing points into a deleted panel)."""
        (self.device.on_status, self.device.on_reply,
         self.device.on_frame) = self._prev_callbacks

    def apply_settings(self):
        """Re-mirror config-driven widgets after the config was edited
        behind the panel's back (host dialogs call this on Apply/OK, and
        the standalone Settings dialog path funnels through here too)."""
        self.live_panel.avg_ms = self.config.bar_avg_ms
        self.live_panel.max_loads = self.config.max_loads
        self.run_panel.history.window_s = self.config.plot_window_s
        self.motion_panel.refresh_span()

    # ── UI ──
    def _build_ui(self):
        self.setStyleSheet(theme.get_stylesheet())
        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)

        self.connect_panel = ConnectPanel(self.config)
        self.connect_panel.connectRequested.connect(self._handle_connect)
        self.connect_panel.disconnectRequested.connect(self._handle_disconnect)
        self.connect_panel.triggerRequested.connect(self.device.trigger_connect)
        root.addWidget(self.connect_panel)
        if self._embedded:                  # host owns connect/disconnect
            self.connect_panel.hide()

        self.tabs = QTabWidget()
        self.live_panel = LivePanel(self.ring)
        self.live_panel.avg_ms = self.config.bar_avg_ms
        self.live_panel.max_loads = self.config.max_loads
        self.live_panel.show_q(self._external_aux)
        self.motion_panel = MotionPanel(self.device)
        self.run_panel = RunPanel(self.device, self.ring)
        self.run_panel.history.window_s = self.config.plot_window_s
        self.run_panel.startDwell.connect(self._begin_dwell)
        self.run_panel.stopDwell.connect(self._end_dwell)
        self.tabs.addTab(self.live_panel, "Live")
        self.tabs.addTab(self.motion_panel, "Motion")
        self.tabs.addTab(self.run_panel, "Run")
        root.addWidget(self.tabs, 1)

        if self._embedded:
            # acquisition timing is suite policy (Measurement Setup owns
            # the sample/dwell parameters) — the OGI average duration must
            # not fork per-device
            self.run_panel.sample_secs.setEnabled(False)
            self.run_panel.sample_secs.setToolTip("set in Measurement Setup")

    # ── connect / disconnect ──
    def _handle_connect(self):
        self.connect_panel.apply_to_config(self.config)
        try:
            self.device.connect()
            self.device.start()
        except OSError as exc:
            self.statusSignal.emit(f"Connect failed: {exc}")
            return
        self.connect_panel.set_state(self.device.connected,
                                     self.device.sim_mode, self.device.link_up)
        self._last_count = 0
        self._last_time = time.perf_counter()

    def _handle_disconnect(self):
        self.device.disconnect()
        self.connect_panel.set_state(False, False, False)

    # ── frame path (IO thread) ──
    def _on_frame(self, bf):
        mf = build_master_frame(bf, alpha=self._alpha, beta=self._beta,
                                q_dyn=self._q)
        self.ring.push(mf)
        self.dwell.add(mf)
        self._latest = mf

    # ── 20 Hz UI refresh (GUI thread) ──
    def _refresh_ui(self):
        # refresh tunnel-q from the aux source (real DaqBook when attached
        # via --daqbook; sim otherwise). Recorded into each frame; shown in
        # the status strip only when the source is a real instrument.
        q = self.aux.dynamic_pressure()
        if q is not None and q > 0:
            self._q = q
        if self._external_aux:
            self.live_panel.set_q(q)
        self.live_panel.refresh()
        self.live_panel.set_rate(self._rate)
        self.run_panel.refresh_plots()
        if self.dwell.active:
            self.run_panel.set_dwell_state(True, self.dwell.n)

    # ── 2 Hz housekeeping ──
    def _slow_tick(self):
        now = time.perf_counter()
        if self._last_time and now > self._last_time:
            dn = self.device.frame_count() - self._last_count
            self._rate = dn / (now - self._last_time)
        self._last_time = now
        self._last_count = self.device.frame_count()

        self.connect_panel.set_state(self.device.connected,
                                     self.device.sim_mode, self.device.link_up)
        self.run_panel.history.note_rate(self._rate)
        # keep position readouts fresh
        if self.device.connected and self.device.link_up:
            self.device.get_positions()

    # ── reply dispatch (GUI thread) ──
    def _handle_reply(self, msg: P.ParsedMessage):
        cmd = msg.command
        fp = msg.float_params()
        if cmd == P.RSP_POSITIONS and len(fp) >= 2:
            yaw, inc = fp[0], fp[1]
            self._beta, self._alpha = yaw, inc
            self.motion_panel.set_positions(yaw, inc)
            self.live_panel.set_positions(yaw, inc)
        elif cmd == P.RSP_YAW_COMPLETE and fp:
            self._beta = fp[0]
            self.motion_panel.set_positions(fp[0], self._alpha)
            self.live_panel.set_positions(fp[0], self._alpha)
        elif cmd == P.RSP_INC_COMPLETE and fp:
            self._alpha = fp[0]
            self.motion_panel.set_positions(self._beta, fp[0])
            self.live_panel.set_positions(self._beta, fp[0])
        elif cmd in (P.RSP_LOCK_STATUS,):
            self.motion_panel.set_lock_status(" ".join(msg.params))
        elif cmd == P.RSP_BAL_LOCKED:
            self.motion_panel.set_lock_status("LOCKED")
        elif cmd == P.RSP_BAL_UNLOCKED:
            self.motion_panel.set_lock_status("UNLOCKED")
        elif cmd == P.RSP_SAMPLES:
            self.run_panel.show_sample("SAMPLES", P.loads_to_named(_pad6(fp)))
        elif cmd == P.RSP_TARES:
            self.run_panel.show_sample("TARES", P.loads_to_named(_pad6(fp)))
        elif cmd == P.RSP_ERROR:
            self.statusSignal.emit("OGI ERROR: " + " ".join(msg.params))
        else:
            self.statusSignal.emit(f"{cmd} {' '.join(msg.params)}".strip())

    # ── dwell ──
    def _begin_dwell(self, alpha: float, beta: float):
        self.dwell.begin(alpha, beta)
        self.run_panel.set_dwell_state(True, 0)

    def _end_dwell(self):
        rp = self.dwell.end()
        self.run_panel.set_dwell_state(False)
        if rp is not None:
            self.run_panel.add_point(rp)
            self.pointSignal.emit(rp)

    # ── teardown ──
    def shutdown(self):
        try:
            self.device.disconnect()
        except Exception as exc:                       # noqa: BLE001
            log.warning("shutdown: %s", exc)
        close = getattr(self.aux, "close", None)
        if callable(close):
            try:
                close()
            except Exception as exc:                   # noqa: BLE001
                log.warning("aux shutdown: %s", exc)

    def closeEvent(self, event):
        self.shutdown()
        super().closeEvent(event)


def _pad6(vals):
    out = list(vals)[:6]
    return out + [0.0] * (6 - len(out))


class AteBalanceMainWindow(QMainWindow):
    def __init__(self, cfg: Optional[AteConfig] = None, aux=None):
        super().__init__()
        self.setWindowTitle("ATE External Balance — TMS Client")
        self.resize(1280, 800)
        # style the shell too, so menus/status bar are dark even when the
        # app-wide stylesheet was not installed (embedded/test use)
        self.setStyleSheet(theme.get_stylesheet())
        self.panel = AteBalancePanel(cfg, aux=aux, parent=self)
        self.setCentralWidget(self.panel)

        sb = QStatusBar()
        self.setStatusBar(sb)
        self._sb_status = QLabel("Idle")
        self._sb_status.setProperty("mono", "true")
        sb.addPermanentWidget(self._sb_status, 1)
        self._sb_mode = QLabel("DISCONNECTED")
        self._sb_mode.setStyleSheet(f"color: {theme.TEXT_DIM}; font-weight: bold;")
        sb.addPermanentWidget(self._sb_mode)
        self._sb_rate = QLabel("— Hz")
        self._sb_rate.setProperty("mono", "true")
        sb.addPermanentWidget(self._sb_rate)

        self.panel.statusSignal.connect(self._sb_status.setText)

        self._mode_timer = QTimer(self)
        self._mode_timer.setInterval(400)
        self._mode_timer.timeout.connect(self._update_mode)
        self._mode_timer.start()

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
            self._apply_settings()

    def _apply_settings(self):
        cfg = self.panel.config
        self.panel.apply_settings()
        self.panel.run_panel.sample_secs.setValue(cfg.default_sample_seconds)
        self.statusBar().showMessage("Settings applied", 3000)

    def _save_cfg(self):
        path, _ = QFileDialog.getSaveFileName(self, "Save config",
                                              "ate_config.json", "JSON (*.json)")
        if path:
            self.panel.connect_panel.apply_to_config(self.panel.config)
            self.panel.config.save(path)

    def _load_cfg(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load config", "",
                                              "JSON (*.json)")
        if path:
            # update the live config IN PLACE — the device (and the panels)
            # hold a reference to this object; rebinding the attribute
            # would silently leave them reading the old config
            loaded = AteConfig.load(path)
            self.panel.config.__dict__.update(loaded.__dict__)
            self._apply_settings()
            self.statusBar().showMessage(f"Loaded {path}", 3000)

    def _update_mode(self):
        dev = self.panel.device
        if not dev.connected:
            self._sb_mode.setText("DISCONNECTED")
            self._sb_mode.setStyleSheet(f"color: {theme.TEXT_DIM}; font-weight: bold;")
        elif dev.sim_mode:
            self._sb_mode.setText("SIMULATION")
            self._sb_mode.setStyleSheet(f"color: {theme.WARNING}; font-weight: bold;")
        elif dev.link_up:
            self._sb_mode.setText("LINKED")
            self._sb_mode.setStyleSheet(f"color: {theme.SUCCESS}; font-weight: bold;")
        else:
            self._sb_mode.setText("WAITING")
            self._sb_mode.setStyleSheet(f"color: {theme.ACCENT_LIGHT}; font-weight: bold;")
        self._sb_rate.setText(f"{self.panel._rate:5.1f} Hz")

    def closeEvent(self, event):
        self.panel.shutdown()
        super().closeEvent(event)
