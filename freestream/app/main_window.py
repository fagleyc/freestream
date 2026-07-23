"""Freestream main window — dock-based GUI shell (spec §7).

Layout: top command bar (mode / SIM-LIVE selector / Connect All /
Start Sweep / Abort / E-STOP / status / SIM badge), left device rail,
center live monitors,
right sweep planner, bottom run log. The sweep engine runs BLOCKING on a
QThread worker; its plain-callable ``SweepCallbacks`` are connected to
pyqtSignal emitters so every UI update lands on the GUI thread. E-STOP
calls ``engine.estop()`` directly from the GUI thread (never queued).
"""

from __future__ import annotations

import importlib
import logging
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from PyQt6.QtCore import QObject, Qt, QThread, pyqtSignal
from PyQt6.QtGui import QAction, QKeySequence, QShortcut
from PyQt6.QtWidgets import (QComboBox, QDialog, QDockWidget, QFileDialog,
                             QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                             QPushButton, QSizePolicy, QTabWidget,
                             QTextBrowser, QToolBar, QVBoxLayout, QWidget)

from .. import about, theme
from ..config import FreestreamConfig
from ..hal import Streaming, capabilities
from ..manager import DeviceManager
from ..recorder import Hdf5Recorder
from ..runsheet import SweepPoint
from ..sweep import (ABORT_SWEEP, OperatorWaitRequest, SweepCallbacks,
                     SweepEngine)
from .console import ConsolePanel
from .device_picker import DevicePickerDialog
from .device_rail import DeviceRail
from .mach_wait_dialog import MachWaitDialog
from .monitors import MonitorPanel
from .planner import PlannerPanel
from .setup_dialog import MeasurementSetupDialog

log = logging.getLogger(__name__)

FAKE_MANIFEST = Path(__file__).resolve().parent / "_fake_manifest.json"

# default dock widths (px) — large enough that the rail cards' status
# pills and the planner's grid columns never clip at the default window
# sizes; re-asserted whenever a collapsed pane slides back in
LEFT_DOCK_WIDTH = 300                 # devices rail
RIGHT_DOCK_WIDTH = 380                # sweep planner


def build_manager(mode: str, sim: bool, on_log=None,
                  custom_devices: Optional[Sequence[str]] = None
                  ) -> DeviceManager:
    """Real adapter manifest by default; bundled fakes as a dev fallback.

    If the real adapters fail to import (not all are written yet), fall
    back to ``_fake_manifest.json`` (freestream._fakes adapters) and say so.
    ``custom_devices`` (non-empty) builds an explicit device subset (Custom
    mode) instead of a manifest mode.
    """
    def _log(msg: str) -> None:
        log.info(msg)
        if on_log:
            on_log(msg)

    try:
        if custom_devices:
            return DeviceManager.custom(list(custom_devices), sim=sim)
        return DeviceManager(mode, sim=sim)
    except Exception as exc:                           # noqa: BLE001
        _log(f"real adapter manifest failed ({exc.__class__.__name__}: "
             f"{exc}) — falling back to bundled FAKE adapters")
        root = Path(__file__).resolve().parents[2]     # project root
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        try:
            if custom_devices:
                return DeviceManager.custom(
                    list(custom_devices), sim=True,
                    manifest_path=FAKE_MANIFEST)
            return DeviceManager(mode, sim=True, manifest_path=FAKE_MANIFEST)
        except Exception:                              # noqa: BLE001
            # the saved custom ids don't exist in the fakes manifest —
            # last-resort default so the window still opens.
            return DeviceManager("SWT-AC-Internal", sim=True,
                                 manifest_path=FAKE_MANIFEST)


class _OperatorWaitTicket:
    """One blocking monitor-only operator wait.

    The ENGINE worker thread blocks on ``event``; the GUI (MachWaitDialog
    resolution, Abort, E-STOP) calls :meth:`resolve`. First decision wins
    (idempotent), so E-STOP's "abort" can never be overwritten by a late
    dialog close — and vice versa."""

    def __init__(self, request: OperatorWaitRequest):
        self.request = request
        self.event = threading.Event()
        self.decision = ABORT_SWEEP                    # safe default
        self._lock = threading.Lock()

    def resolve(self, decision: str) -> None:
        with self._lock:
            if not self.event.is_set():
                self.decision = decision
                self.event.set()


class _SweepRunner(QObject):
    """QThread worker wrapping the BLOCKING SweepEngine.run/run_point.

    SweepCallbacks fields are plain callables — they are pointed at this
    object's signal ``emit`` methods, so callback invocations on the
    worker thread arrive in the GUI thread as queued signals.
    """

    # NB: must not be named "event" — that would shadow QObject.event()
    # (the virtual event handler) and break event delivery to the worker.
    logEvent = pyqtSignal(str)
    pointState = pyqtSignal(int, str)
    pointDone = pyqtSignal(object)                     # PointOutcome
    finishedRun = pyqtSignal(object)                   # List[PointOutcome]
    operatorWait = pyqtSignal(object)                  # _OperatorWaitTicket
    pausedHold = pyqtSignal(int, int)                  # (next_index, total)
    resumedRun = pyqtSignal()
    done = pyqtSignal()

    def __init__(self, points: List[SweepPoint],
                 single_index: Optional[int] = None):
        super().__init__()
        self._engine: Optional[SweepEngine] = None     # set via bind()
        self._points = points
        self._single_index = single_index

    def bind(self, engine: SweepEngine) -> None:
        """Attach the engine (built AFTER the runner so its callbacks can
        point at this runner's signal emitters)."""
        self._engine = engine

    def request_operator_wait(self, request: OperatorWaitRequest) -> str:
        """Engine hook (runs on the WORKER thread): marshal the request to
        the GUI thread as a queued signal, then BLOCK until the dialog —
        or Abort/E-STOP — resolves the ticket. No timeout: the ticket is
        guaranteed released by the main window's abort paths."""
        ticket = _OperatorWaitTicket(request)
        self.operatorWait.emit(ticket)
        ticket.event.wait()
        return ticket.decision

    def run(self) -> None:
        try:
            if self._single_index is None:
                self._engine.run(self._points)         # cb.on_finished fires
            else:
                outcome = self._engine.run_point(
                    self._single_index, self._points[0])
                self.finishedRun.emit([outcome])
        except Exception as exc:                       # noqa: BLE001
            log.exception("sweep worker crashed")
            self.logEvent.emit(f"sweep worker error: {exc}")
        finally:
            self.done.emit()


class PaneHandle(QPushButton):
    """Slim full-height triangle handle at the edge of the central pane —
    click to slide the adjacent dock (device rail / sweep planner) in and
    out. Checked = pane visible; the triangle points where clicking will
    move the pane."""

    def __init__(self, side: str, tooltip: str, parent=None):
        super().__init__(parent)
        self._side = side                              # "left" | "right"
        self.setCheckable(True)
        self.setChecked(True)
        self.setToolTip(tooltip)
        self.setFixedWidth(14)
        self.setSizePolicy(QSizePolicy.Policy.Fixed,
                           QSizePolicy.Policy.Expanding)
        self.setStyleSheet(
            "QPushButton { background: transparent; border: none; "
            f"color: {theme.TEXT_DIM}; font-size: 8pt; padding: 0; }}\n"
            "QPushButton:hover { background: "
            f"{theme.SURFACE}; color: {theme.ACCENT_LIGHT}; "
            "border-radius: 3px; }")
        self.toggled.connect(self._update_arrow)
        self._update_arrow(self.isChecked())

    def _update_arrow(self, open_: bool) -> None:
        if self._side == "left":
            self.setText("◀" if open_ else "▶")        # collapse | expand
        else:
            self.setText("▶" if open_ else "◀")


class AboutDialog(QDialog):
    """Shared-template About dialog: name + version prominent, one-
    paragraph summary, author/contact line, compact version-history
    table. Dark theme comes from the app stylesheet."""

    def __init__(self, parent=None):
        super().__init__(parent)
        import html as _html
        self.setWindowTitle(f"About {about.APP_NAME}")
        self.setFixedSize(560, 520)

        v = QVBoxLayout(self)
        v.setSpacing(10)

        title = QLabel(about.APP_NAME)
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        title.setStyleSheet("font-size: 20pt; font-weight: bold;")
        v.addWidget(title)

        ver = QLabel(f"Version {about.__version__}")
        ver.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ver.setStyleSheet(f"color: {theme.TEXT_DIM};")
        v.addWidget(ver)

        summary = QLabel(about.SUMMARY)
        summary.setWordWrap(True)
        v.addWidget(summary)

        author = QLabel(f"Author: {about.AUTHOR} — {about.CONTACT}")
        author.setAlignment(Qt.AlignmentFlag.AlignCenter)
        author.setStyleSheet(f"color: {theme.TEXT_DIM};")
        v.addWidget(author)

        rows = "".join(
            "<tr>"
            f"<td style='padding:2px 10px 2px 0; white-space:nowrap;'>"
            f"<b>{_html.escape(version)}</b></td>"
            f"<td style='padding:2px 10px 2px 0; white-space:nowrap;"
            f" color:{theme.TEXT_DIM};'>{_html.escape(iso_date)}</td>"
            f"<td style='padding:2px 0;'>{_html.escape(note)}</td>"
            "</tr>"
            for version, iso_date, note in about.VERSION_HISTORY
        )
        hist = QTextBrowser()
        hist.setHtml("<table cellspacing='0' cellpadding='0'>"
                     + rows + "</table>")
        v.addWidget(hist, 1)

        btn = QPushButton("Close")
        btn.clicked.connect(self.accept)
        v.addWidget(btn, alignment=Qt.AlignmentFlag.AlignCenter)


class FreestreamMainWindow(QMainWindow):
    """Dock-based shell. Pass ``manager`` to inject a prebuilt
    DeviceManager (tests); otherwise one is built from the config via
    :func:`build_manager` (real manifest first, fakes fallback)."""

    def __init__(self, config: Optional[FreestreamConfig] = None,
                 manager: Optional[DeviceManager] = None):
        super().__init__()
        self.config = config or FreestreamConfig()
        self.setWindowTitle("Freestream — Wind Tunnel Suite")
        self.resize(1500, 950)
        self.setStyleSheet(theme.get_stylesheet())

        startup_msgs: List[str] = []
        self.manager = manager or build_manager(
            self.config.mode, self.config.sim, startup_msgs.append,
            custom_devices=(self.config.custom_devices
                            if self.config.mode == DeviceManager.CUSTOM
                            else None))
        self.recorder = self._make_recorder()
        self.engine: Optional[SweepEngine] = None
        self._connected = False
        self._running = False
        self._paused_msg: Optional[str] = None   # "SWEEP PAUSED — …" status
        self._thread: Optional[QThread] = None
        self._runner: Optional[_SweepRunner] = None
        self._active_points: List[SweepPoint] = []
        # pending monitor-only operator wait (engine thread blocked on it)
        self._wait_ticket: Optional[_OperatorWaitTicket] = None
        self._wait_dialog: Optional[MachWaitDialog] = None

        self._build_command_bar()
        self._build_central()
        self._build_docks()
        self._build_menus()
        self._update_ui_state()
        if self.config.device_configs:
            # startup defaults / passed-in config carry saved driver
            # configs (ranges, rates, resolutions) — apply them now
            self._apply_device_configs()

        for msg in startup_msgs:
            self.console.log(msg)
        self.console.log(
            f"registry [{self.manager.mode}]: "
            + ", ".join(self.manager.devices) + f"  (manifest: "
            f"{self.manager.manifest_path.name})")

        QShortcut(QKeySequence(Qt.Key.Key_Escape), self,
                  activated=self._estop)
        QShortcut(QKeySequence("Ctrl+1"), self,
                  activated=self.left_handle.toggle)
        QShortcut(QKeySequence("Ctrl+2"), self,
                  activated=self.right_handle.toggle)

    # ── construction ─────────────────────────────────────────────────────
    @staticmethod
    def _bar_spacer() -> QWidget:
        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Policy.Expanding,
                             QSizePolicy.Policy.Preferred)
        return spacer

    def _build_command_bar(self) -> None:
        """Mode selector left · Connect-All → E-STOP cluster CENTERED ·
        SIM/LIVE selector + status by the right header."""
        bar = QToolBar("Command")
        bar.setMovable(False)
        bar.setFloatable(False)
        self.addToolBar(Qt.ToolBarArea.TopToolBarArea, bar)

        mode_lbl = QLabel("Mode")   # toolbar spacing comes from the theme
        bar.addWidget(mode_lbl)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(list(self.manager.manifest["modes"]))
        self.mode_combo.addItem(DeviceManager.CUSTOM)   # pick devices by hand
        self.mode_combo.setToolTip(
            "SWT-AC-Internal — crescent sting + StrainBook internal "
            "balance + DaqBook + tunnel PLC.\n"
            "SWT-External — ATE external balance rig + DaqBook + tunnel "
            "PLC.\n"
            "SWT-Traverse — traverse X/Y/Z matrix + DaqBook.\n"
            "LSWT-LSWTSting-NI — LSWT sting + NI USB-6351 balance DAQ + "
            "Heise (Ptot/Temp) + LSWT fan drive (North tunnel).\n"
            "'custom' opens a picker to choose any device subset.\n"
            "Switchable only while disconnected.")
        self.mode_combo.setCurrentText(self.manager.mode)
        self.mode_combo.currentTextChanged.connect(self._on_mode_changed)
        bar.addWidget(self.mode_combo)

        # centered command cluster
        bar.addWidget(self._bar_spacer())

        self.connect_btn = QPushButton("Connect All")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.setMinimumSize(140, 34)
        self.connect_btn.clicked.connect(self._toggle_connect)
        bar.addWidget(self.connect_btn)

        self.start_btn = QPushButton("Start Sweep")
        self.start_btn.setObjectName("success")
        self.start_btn.setMinimumSize(120, 34)
        self.start_btn.clicked.connect(self._start_sweep)
        bar.addWidget(self.start_btn)

        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setMinimumSize(90, 34)
        self.pause_btn.setToolTip("Pause after current point")
        self.pause_btn.clicked.connect(self._toggle_pause)
        bar.addWidget(self.pause_btn)

        self.abort_btn = QPushButton("Abort")
        self.abort_btn.setMinimumSize(80, 34)
        self.abort_btn.clicked.connect(self._abort)
        bar.addWidget(self.abort_btn)

        self.estop_btn = QPushButton("E-STOP")
        self.estop_btn.setObjectName("danger")         # always red
        self.estop_btn.setMinimumSize(120, 40)
        self.estop_btn.clicked.connect(self._estop)
        bar.addWidget(self.estop_btn)

        bar.addWidget(self._bar_spacer())

        # SIM/LIVE selector — by the right header, next to its badge.
        # Only enabled while disconnected; rebuilds the DeviceManager.
        self.sim_combo = QComboBox()
        self.sim_combo.addItems(["SIM", "LIVE"])
        self.sim_combo.setCurrentText("SIM" if self.manager.sim else "LIVE")
        self.sim_combo.setToolTip(
            "SIM — simulated adapters (device emulators; no hardware is "
            "touched).\n"
            "LIVE — real hardware over the configured interfaces.\n"
            "Switchable only while disconnected; the choice is saved with "
            "the config.\n(--sim / --live on the command line still work.)")
        self.sim_combo.currentTextChanged.connect(self._on_sim_changed)
        bar.addWidget(self.sim_combo)

        self.status_lbl = QLabel("disconnected")
        self.status_lbl.setStyleSheet(f"color: {theme.TEXT_DIM}; "
                                      "padding: 0 10px;")
        bar.addWidget(self.status_lbl)

        self.sim_badge = QLabel()
        bar.addWidget(self.sim_badge)
        self._update_sim_badge()

    def _update_sim_badge(self) -> None:
        """Keep the SIM/LIVE badge in sync with the active manager."""
        self.sim_badge.setText("SIM" if self.manager.sim else "LIVE")
        self.sim_badge.setStyleSheet(
            "background: {bg}; color: white; border-radius: 8px; "
            "padding: 3px 12px; font-weight: bold;".format(
                bg=theme.ACCENT_DARK if self.manager.sim else theme.ERROR))

    def _build_central(self) -> None:
        central = QWidget()
        central.setObjectName("root")
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 4, 0, 4)
        outer.setSpacing(2)

        # triangle slide-out handles hug the central pane's edges
        self.left_handle = PaneHandle(
            "left", "Slide the device rail in/out  (Ctrl+1)")
        self.left_handle.toggled.connect(self._toggle_left_pane)
        outer.addWidget(self.left_handle)

        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(4)
        # refuse-to-record banner (spec hard requirement)
        self.banner = QLabel("")
        self.banner.setObjectName("blockerBanner")
        self.banner.setWordWrap(True)
        self.banner.setStyleSheet(
            f"QLabel#blockerBanner {{ background: {theme.ERROR}; "
            f"color: white; font-weight: bold; padding: 8px; "
            f"border-radius: 4px; }}")
        self.banner.hide()
        lay.addWidget(self.banner)
        # sibling INFO banner (non-error): shown for a device set with no
        # data-acquisition device (Mode 3 traverse-only, or a custom set
        # with no streaming) — manual positioning is fine, but there is
        # nothing to record so an automated sweep is disabled, NOT faulted.
        self.info_banner = QLabel("")
        self.info_banner.setObjectName("infoBanner")
        self.info_banner.setWordWrap(True)
        self.info_banner.setStyleSheet(
            f"QLabel#infoBanner {{ background: {theme.SURFACE}; "
            f"color: {theme.TEXT}; border: 1px solid {theme.ACCENT}; "
            f"padding: 8px; border-radius: 4px; }}")
        self.info_banner.hide()
        lay.addWidget(self.info_banner)
        self.monitors = MonitorPanel(self.manager, self.config)
        # explicit minimum CAP: a QTabWidget's minimum is the max over all
        # pages, so one wide panel would otherwise force the central
        # widget under the Devices dock. Pages clip internally instead.
        self.monitors.setMinimumWidth(320)
        lay.addWidget(self.monitors, stretch=1)

        outer.addWidget(inner, 1)
        self.right_handle = PaneHandle(
            "right", "Slide the sweep planner in/out  (Ctrl+2)")
        self.right_handle.toggled.connect(self._toggle_right_pane)
        outer.addWidget(self.right_handle)
        self.setCentralWidget(central)
        # live balance overstress refuses recording (spec §6.2)
        self.manager.extra_blockers.append(
            self.monitors.forces.record_blocker)
        # the Forces panel INHERITS .vol/fit/layout from the balance
        # device; its "Balance device…" button opens the ONE canonical
        # editor (the StrainBook device panel's Forces tab)
        self.monitors.forces.configureBalanceRequested.connect(
            self._open_balance_device)

    def _build_docks(self) -> None:
        # GroupedDragging: docks tabbed together (e.g. Devices + Sweep
        # Planner) drag OUT as one merged floating window instead of
        # tearing apart; nested/tabbed layouts stay enabled.
        self.setDockOptions(
            QMainWindow.DockOption.AnimatedDocks
            | QMainWindow.DockOption.AllowNestedDocks
            | QMainWindow.DockOption.AllowTabbedDocks
            | QMainWindow.DockOption.GroupedDragging)
        # tabified docks get their tab bar on the OUTER side of the panel
        # (left area → West, right area → East) instead of underneath
        self.setTabPosition(Qt.DockWidgetArea.LeftDockWidgetArea,
                            QTabWidget.TabPosition.West)
        self.setTabPosition(Qt.DockWidgetArea.RightDockWidgetArea,
                            QTabWidget.TabPosition.East)
        self.rail = DeviceRail(self.manager)
        self.rail.deviceClicked.connect(self._open_device_settings)
        self.rail.deviceConnectRequested.connect(self._connect_device)
        self.rail.deviceDisconnectRequested.connect(self._disconnect_device)
        dock = QDockWidget("Devices", self)
        dock.setObjectName("devicesDock")
        dock.setWidget(self.rail)
        dock.setMinimumWidth(260)
        self.devices_dock = dock
        dock.visibilityChanged.connect(
            lambda v: self._sync_pane_toggle(self.left_handle, v))
        dock.topLevelChanged.connect(
            lambda f, d=dock: self._on_dock_floated(d, f))
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)

        self.planner = PlannerPanel(self.config)
        self.planner.message.connect(lambda m: self.console.log(m))
        self.planner.rerunRequested.connect(self._rerun_point)
        self.planner.runApplied.connect(self._on_run_applied)
        self.planner.set_axis_mode(self._planner_axis_mode())
        dock = QDockWidget("Sweep Planner", self)
        dock.setObjectName("plannerDock")
        dock.setWidget(self.planner)
        dock.setMinimumWidth(340)
        self.planner_dock = dock
        dock.visibilityChanged.connect(
            lambda v: self._sync_pane_toggle(self.right_handle, v))
        dock.topLevelChanged.connect(
            lambda f, d=dock: self._on_dock_floated(d, f))
        self.addDockWidget(Qt.DockWidgetArea.RightDockWidgetArea, dock)

        # sane default dock widths: the rail's sizeHint (~380 px from the
        # widest card text) would otherwise claim width the dashboard's
        # top band needs at the default window size
        self.resizeDocks([self.devices_dock, self.planner_dock],
                         [LEFT_DOCK_WIDTH, RIGHT_DOCK_WIDTH],
                         Qt.Orientation.Horizontal)

        self.console = ConsolePanel()
        dock = QDockWidget("Run Log", self)
        dock.setObjectName("consoleDock")
        dock.setWidget(self.console)
        dock.setMinimumHeight(120)
        dock.topLevelChanged.connect(
            lambda f, d=dock: self._on_dock_floated(d, f))
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, dock)

    def _build_menus(self) -> None:
        file_menu = self.menuBar().addMenu("&File")

        act = QAction("&Save Config…", self)
        act.triggered.connect(self._save_config)
        file_menu.addAction(act)
        act = QAction("&Load Config…", self)
        act.triggered.connect(self._load_config)
        file_menu.addAction(act)
        act = QAction("Set Current as &Defaults", self)
        act.setToolTip(
            "Store ALL current settings (measurement setup, sample rate, "
            "directories, output format + every device's ranges/rates/"
            "resolutions) as the startup defaults — auto-loaded on the "
            "next launch. Separate from Save/Load Config files.")
        act.triggered.connect(self._save_defaults)
        file_menu.addAction(act)
        file_menu.addSeparator()
        act = QAction("&Import Run Sheet…", self)
        act.triggered.connect(self.planner._import_clicked)
        act.setToolTip("Load a run-sheet workbook and pick a run to execute")
        file_menu.addAction(act)
        act = QAction("&Measurement Setup…", self)
        act.triggered.connect(self._open_setup)
        file_menu.addAction(act)
        file_menu.addSeparator()
        act = QAction("E&xit", self)
        act.triggered.connect(self.close)
        file_menu.addAction(act)

        # Devices menu — rebuilt on open so it tracks the active mode's
        # registry; one submenu per device with the same actions as the
        # rail cards (Configure / Connect / Disconnect).
        self.devices_menu = self.menuBar().addMenu("&Devices")
        self.devices_menu.aboutToShow.connect(self._fill_devices_menu)

        # Advanced — specialist tools that live outside the sweep workflow
        adv_menu = self.menuBar().addMenu("&Advanced")
        act = QAction("Balance &Calibration…", self)
        act.setToolTip("Open the balance_cal .vol-acquisition window "
                       "(balcal_gui). Shares the live StrainBook when "
                       "Freestream is connected; standalone otherwise.")
        act.triggered.connect(self._open_balance_cal)
        adv_menu.addAction(act)

        # Help — always the LAST menu in the bar
        help_menu = self.menuBar().addMenu("&Help")
        act = QAction("&Documentation", self)
        act.triggered.connect(self._open_documentation)
        help_menu.addAction(act)
        help_menu.addSeparator()
        act = QAction(f"&About {about.APP_NAME}", self)
        act.triggered.connect(self._show_about)
        help_menu.addAction(act)

    def _open_documentation(self) -> None:
        """Help ▸ Documentation — open docs/index.html in the browser."""
        import webbrowser
        docs = Path(__file__).resolve().parents[2] / "docs" / "index.html"
        if not docs.is_file():
            QMessageBox.warning(self, "Not Found",
                                f"Documentation not found at:\n{docs}")
            return
        webbrowser.open(docs.as_uri())

    def _show_about(self) -> None:
        """Help ▸ About — shared-template About dialog."""
        dlg = AboutDialog(self)
        dlg.exec()

    def _open_balance_cal(self) -> None:
        """Advanced ▸ Balance Calibration — the balcal_gui window.

        One window per session (re-invocation raises the existing one).
        When Freestream is CONNECTED with the StrainBook as the balance,
        the window SHARES the live driver — one device, one connection —
        otherwise it owns its own DAQ backend selection."""
        win = getattr(self, "_balcal_win", None)
        if win is not None:
            win.show()
            win.raise_()
            win.activateWindow()
            return
        balcal_dir = Path(__file__).resolve().parents[2] / "utilities" / "balance_cal"
        if str(balcal_dir) not in sys.path:
            sys.path.insert(0, str(balcal_dir))
        try:
            from balcal_gui.app.main_window import BalanceCalWindow
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.warning(self, "Balance Calibration",
                                f"balcal_gui unavailable: {exc}")
            self.console.log(f"balance cal window failed to import: {exc}")
            return
        shared = None
        bal = self.manager.by_role("balance")
        if (self._connected and bal is not None
                and getattr(bal, "id", "") == "strainbook"):
            shared = getattr(bal, "driver", None)
        self._balcal_win = BalanceCalWindow(device=shared,
                                            sim=self.manager.sim)
        self._balcal_win.show()
        self.console.log(
            "balance calibration window opened"
            + (" — sharing the live StrainBook" if shared is not None
               else " (standalone DAQ)"))

    def _fill_devices_menu(self) -> None:
        self.devices_menu.clear()
        for dev_id, dev in self.manager.devices.items():
            sub = self.devices_menu.addMenu(getattr(dev, "label", dev_id))
            act = sub.addAction("Configure…")
            act.triggered.connect(
                lambda _c=False, d=dev_id: self._open_device_settings(d))
            act.setEnabled(callable(getattr(dev, "config_dict", None)))
            sub.addSeparator()
            if dev.connected:
                act = sub.addAction("Disconnect device")
                act.triggered.connect(
                    lambda _c=False, d=dev_id: self._disconnect_device(d))
            else:
                act = sub.addAction("Connect device")
                act.triggered.connect(
                    lambda _c=False, d=dev_id: self._connect_device(d))

    # ── state ────────────────────────────────────────────────────────────
    @property
    def sweep_active(self) -> bool:
        return self._running

    def _update_ui_state(self) -> None:
        self.mode_combo.setEnabled(not self._connected)
        self.sim_combo.setEnabled(not self._connected)
        self.connect_btn.setText("Disconnect All" if self._connected
                                 else "Connect All")
        self.connect_btn.setEnabled(not self._running)
        # no Streaming device → nothing to record → no automated sweep
        # (Mode 3 traverse-only or a custom set with no data device)
        has_data = bool(self.manager.streaming)
        self.start_btn.setEnabled(self._connected and not self._running
                                  and has_data)
        # Pause toggles Pause⇄Resume while a sweep runs; disabled otherwise
        self.pause_btn.setEnabled(self._running)
        if not self._running:
            self._sync_pause_btn()
        self.abort_btn.setEnabled(self._running)
        # Clear Grid must be disabled while the plan is executing —
        # and STAYS locked while the sweep is paused (still running)
        self.planner.set_sweep_running(self._running)
        self.monitors.active = self._connected
        self._refresh_mode_banner()
        if self._running:
            self.status_lbl.setText(self._paused_msg or "SWEEP RUNNING")
        elif self._connected:
            self.status_lbl.setText("connected — idle")
        else:
            self.status_lbl.setText("disconnected")

    def _refresh_mode_banner(self) -> None:
        """Show the non-error info banner (and keep Start disabled) when the
        active device set has no data-acquisition device to record."""
        if self.manager.streaming:
            self.info_banner.hide()
            self.start_btn.setToolTip("")
            return
        pos = self.manager.positioner
        if pos is not None:
            names = "/".join(a.name for a in pos.axes())
            self.info_banner.setText(
                f"Traverse-only mode — manual positioning via the "
                f"{getattr(pos, 'label', getattr(pos, 'id', 'positioner'))} "
                f"panel ({names}); no data-acquisition devices to record, "
                f"so Start Sweep is disabled.")
        else:
            self.info_banner.setText(
                "This device set has no data-acquisition (streaming) "
                "devices — nothing to record, so Start Sweep is disabled.")
        self.info_banner.show()
        self.start_btn.setToolTip(
            "No data-acquisition device in this set — nothing to record.")

    # ── collapsible panes ────────────────────────────────────────────────
    def _toggle_left_pane(self, visible: bool) -> None:
        self.devices_dock.setVisible(visible)
        if visible and not self.devices_dock.isFloating():
            # re-assert the width: a re-shown dock otherwise reopens at
            # whatever squeezed width the layout last left it (clipped
            # under the central frame's minimum)
            self.resizeDocks([self.devices_dock], [LEFT_DOCK_WIDTH],
                             Qt.Orientation.Horizontal)

    def _toggle_right_pane(self, visible: bool) -> None:
        self.planner_dock.setVisible(visible)
        if visible and not self.planner_dock.isFloating():
            self.resizeDocks([self.planner_dock], [RIGHT_DOCK_WIDTH],
                             Qt.Orientation.Horizontal)

    @staticmethod
    def _on_dock_floated(dock: QDockWidget, floating: bool) -> None:
        """Give torn-off docks a REAL window frame (minimize/maximize/
        close) — by default a floating QDockWidget can't be minimized."""
        if floating:
            dock.setWindowFlags(
                Qt.WindowType.Window
                | Qt.WindowType.CustomizeWindowHint
                | Qt.WindowType.WindowTitleHint
                | Qt.WindowType.WindowMinMaxButtonsHint
                | Qt.WindowType.WindowCloseButtonHint)
            dock.show()                                # flags change re-hides

    def _sync_pane_toggle(self, button, visible: bool) -> None:
        """Keep a toggle button in sync when a dock is closed via its X."""
        if button.isChecked() != visible:
            button.blockSignals(True)
            button.setChecked(visible)
            button.blockSignals(False)

    # ── connect / mode ───────────────────────────────────────────────────
    def _toggle_connect(self) -> None:
        if self._connected:
            if self._running:
                self.console.log("cannot disconnect while a sweep runs")
                return
            for s in self.manager.streaming:
                try:
                    s.stop()
                except Exception:                      # noqa: BLE001
                    pass
            self.manager.disconnect_all()
            self._connected = False
            self.console.log("all devices disconnected")
        else:
            self._push_sample_rate()             # one suite-wide rate
            errors = self.manager.connect_all()
            for dev_id, exc in errors.items():
                self.console.log(f"connect {dev_id} FAILED: {exc}")
            for s in self.manager.streaming:
                try:
                    s.start()
                except Exception as exc:               # noqa: BLE001
                    self.console.log(f"stream start failed: {exc}")
            self._connected = True
            ok = len(self.manager.devices) - len(errors)
            self.console.log(f"connected {ok}/{len(self.manager.devices)} "
                             "devices; streams started")
        self.rail.poll()
        self._update_ui_state()

    def _on_mode_changed(self, mode: str) -> None:
        if self._connected:                            # combo is disabled;
            self._set_mode_combo(self.manager.mode)    # belt & braces
            return
        if mode == DeviceManager.CUSTOM:               # always (re)pick
            self._enter_custom_mode()
            return
        if mode == self.manager.mode:
            return
        try:
            mgr = DeviceManager(mode, sim=self.manager.sim,
                                manifest_path=self.manager.manifest_path)
        except Exception as exc:                       # noqa: BLE001
            self.console.log(f"mode switch to {mode} failed: {exc}")
            self._set_mode_combo(self.manager.mode)
            return
        self._adopt_manager(mgr)
        self.config.mode = mode
        self.config.custom_devices = []                # leaving custom mode
        self.console.log(f"mode → {mode}: devices "
                         + ", ".join(mgr.devices))

    # ── custom mode (pick devices one by one) ────────────────────────────
    def _enter_custom_mode(self) -> None:
        """Open the device picker; on accept, build a manager from EXACTLY
        the ticked subset (roles inferred from capabilities)."""
        catalog = self._device_catalog()
        preselected = (self.config.custom_devices
                       or list(self.manager.devices))
        dlg = DevicePickerDialog(catalog, preselected, self)
        if not dlg.exec():                             # cancelled → revert
            self._set_mode_combo(self.manager.mode)
            return
        chosen = dlg.selected_devices()
        try:
            self._build_and_adopt_custom(chosen)
        except Exception as exc:                       # noqa: BLE001
            self.console.log(f"custom mode build failed: {exc}")
            self._set_mode_combo(self.manager.mode)

    def _build_and_adopt_custom(self, device_ids: Sequence[str]) -> None:
        mgr = DeviceManager.custom(list(device_ids), sim=self.manager.sim,
                                   manifest_path=self.manager.manifest_path)
        self._adopt_manager(mgr)
        self.config.mode = DeviceManager.CUSTOM
        self.config.custom_devices = list(device_ids)
        self._set_mode_combo(DeviceManager.CUSTOM)
        self.console.log("custom mode: devices " + ", ".join(mgr.devices)
                         + "  (" + self._roles_summary(mgr) + ")")

    def _device_catalog(self
                        ) -> Dict[str, Tuple[str, List[str]]]:
        """id → (label, capability-tags) for EVERY device in the manifest
        registry, built by instantiating each adapter once in sim. Failures
        (driver not importable) still list the id with no tags."""
        catalog: Dict[str, Tuple[str, List[str]]] = {}
        for dev_id, entry in self.manager.manifest["devices"].items():
            try:
                module_name, cls_name = entry["adapter"].rsplit(".", 1)
                cls = getattr(importlib.import_module(module_name), cls_name)
                adapter = cls(sim=True, **entry.get("options", {}))
                adapter.id = dev_id
                catalog[dev_id] = (getattr(adapter, "label", dev_id),
                                   capabilities(adapter))
            except Exception as exc:                   # noqa: BLE001
                self.console.log(f"device {dev_id} unavailable for the "
                                 f"custom picker: {exc}")
                catalog[dev_id] = (dev_id, [])
        return catalog

    @staticmethod
    def _roles_summary(mgr: DeviceManager) -> str:
        parts = []
        if mgr.positioner is not None:
            parts.append(f"positioner={mgr.positioner.id}")
        if mgr.setpoint is not None:
            parts.append(f"tunnel={mgr.setpoint.id}")
        streams = [getattr(s, "id", "?") for s in mgr.streaming]
        parts.append("streaming=" + (", ".join(streams) or "none"))
        return "; ".join(parts)

    def _on_sim_changed(self, text: str) -> None:
        """SIM/LIVE selector: rebuild the DeviceManager with the new flag."""
        sim = text == "SIM"
        if sim == self.manager.sim:
            return
        if self._connected:                            # combo is disabled;
            self._set_sim_combo(self.manager.sim)      # belt & braces
            return
        try:
            if self.manager.custom_devices is not None:
                mgr = DeviceManager.custom(
                    self.manager.custom_devices, sim=sim,
                    manifest_path=self.manager.manifest_path)
            else:
                mgr = DeviceManager(self.manager.mode, sim=sim,
                                    manifest_path=self.manager.manifest_path)
        except Exception as exc:                       # noqa: BLE001
            self.console.log(f"switch to {text} failed: {exc}")
            self._set_sim_combo(self.manager.sim)
            return
        self._adopt_manager(mgr)
        self.config.sim = sim                          # persists on save
        self.console.log(f"adapters → {text}: devices "
                         + ", ".join(mgr.devices))

    def _adopt_manager(self, mgr: DeviceManager) -> None:
        """Swap in a freshly built DeviceManager (mode or SIM/LIVE switch)."""
        self.manager = mgr
        self.engine = None
        self._apply_device_configs()          # carry saved driver configs
        mgr.extra_blockers.append(self.monitors.forces.record_blocker)
        self.rail.set_manager(mgr)
        self.monitors.set_manager(mgr)
        # planner axes follow the positioner: traverse → X/Y/Z matrix,
        # sting rigs → alpha/beta/mach attitude sweep
        self.planner.set_axis_mode(self._planner_axis_mode())
        self._update_sim_badge()
        self._update_ui_state()                # start-btn + info banner

    def _planner_axis_mode(self) -> str:
        """"xyz" when the active Positioner exposes traverse axes."""
        pos = self.manager.positioner
        if pos is not None:
            try:
                if {a.name for a in pos.axes()} & {"x", "y", "z"}:
                    return "xyz"
            except Exception:                          # noqa: BLE001
                pass
        return "aero"

    def _set_mode_combo(self, mode: str) -> None:
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentText(mode)
        self.mode_combo.blockSignals(False)

    def _set_sim_combo(self, sim: bool) -> None:
        self.sim_combo.blockSignals(True)
        self.sim_combo.setCurrentText("SIM" if sim else "LIVE")
        self.sim_combo.blockSignals(False)

    # ── unified sample rate ──────────────────────────────────────────────
    def _push_sample_rate(self) -> None:
        """Push the ONE suite-wide sample rate into every streaming adapter
        that supports it (applied by the drivers at connect). Adapters with
        a fixed rate keep reporting it honestly and are just logged."""
        hz = float(self.config.sample_rate_hz)
        for s in self.manager.streaming:
            dev_id = getattr(s, "id", "?")
            setter = getattr(s, "set_sample_rate", None)
            if callable(setter):
                try:
                    setter(hz)
                except Exception as exc:               # noqa: BLE001
                    self.console.log(f"{dev_id}: set sample rate failed: "
                                     f"{exc}")
            else:
                try:
                    fixed = s.sample_rate()
                    self.console.log(f"{dev_id}: fixed rate {fixed:g} Hz "
                                     f"(cannot follow the global "
                                     f"{hz:g} Hz)")
                except Exception:                      # noqa: BLE001
                    pass

    # ── sweep control ────────────────────────────────────────────────────
    def _start_sweep(self) -> None:
        if self._running:
            return
        if not self.manager.streaming:
            self.info_banner.show()
            self.console.log("start refused — no data-acquisition devices "
                             "in this set; use the embedded positioner "
                             "panel for manual moves")
            return
        points = self.planner.points
        if not points:
            self.console.log("no points — build a grid or import a "
                             "run sheet first")
            return
        blockers = self.manager.record_blockers()
        if blockers:                                   # spec hard req: refuse
            self.banner.setText("RECORDING BLOCKED — "
                                + ";  ".join(blockers))
            self.banner.show()
            self.console.log("start refused — blockers: "
                             + "; ".join(blockers))
            return
        self.banner.hide()
        for p in points:
            p.status = "queued"
        self.planner.refresh_statuses()
        self._launch(points)

    def _rerun_point(self, row: int) -> None:
        if self._running:
            self.console.log("re-run refused — a sweep is already running")
            return
        blockers = self.manager.record_blockers()
        if blockers:
            self.banner.setText("RECORDING BLOCKED — "
                                + ";  ".join(blockers))
            self.banner.show()
            self.console.log("re-run refused — blockers: "
                             + "; ".join(blockers))
            return
        self.banner.hide()
        point = self.planner.points[row]
        desc = ", ".join(
            f"{n}={getattr(point, n):g}"
            for n in ("alpha", "beta", "mach", "x", "y", "z")
            if getattr(point, n) is not None) or "no axes"
        self.console.log(f"re-running point {row} ({desc})")
        self._launch([point], single_index=row)

    def _launch(self, points: List[SweepPoint],
                single_index: Optional[int] = None) -> None:
        # fresh recorder so config edits (data_root/config_name) apply
        self.recorder = self._make_recorder()
        runner = _SweepRunner(points, single_index)
        callbacks = SweepCallbacks(
            on_event=runner.logEvent.emit,
            on_point_state=runner.pointState.emit,
            on_point_done=runner.pointDone.emit,
            on_finished=runner.finishedRun.emit,
            on_operator_wait=runner.request_operator_wait,
            on_paused=runner.pausedHold.emit,
            on_resumed=runner.resumedRun.emit)
        self.engine = SweepEngine(self.manager, self.recorder,
                                  self.config, callbacks)
        runner.bind(self.engine)
        self._active_points = points

        thread = QThread(self)
        runner.moveToThread(thread)
        thread.started.connect(runner.run)
        runner.done.connect(thread.quit)
        runner.logEvent.connect(self.console.log)
        runner.operatorWait.connect(self._on_operator_wait)
        runner.pausedHold.connect(self._on_sweep_paused)
        runner.resumedRun.connect(self._on_sweep_resumed)
        runner.pointState.connect(self._on_point_state)
        runner.pointDone.connect(self.planner.mark_done)
        runner.pointDone.connect(self._on_point_done)
        runner.finishedRun.connect(self._on_finished)
        thread.finished.connect(self._on_thread_finished)

        self._thread, self._runner = thread, runner
        self._running = True
        self._paused_msg = None
        self._sync_pause_btn()
        self._update_ui_state()
        n = len(points)
        self.console.log(f"sweep started: {n} point"
                         + ("s" if n != 1 else ""))
        thread.start()

    # ── pause / resume (point-boundary; abort & E-STOP always win) ──────
    def _toggle_pause(self) -> None:
        """Pause⇄Resume while a sweep runs. Pause takes effect at the
        NEXT point boundary (the current point finishes normally)."""
        if self.engine is None or not self._running:
            return
        if self.engine.pause_requested:
            self.engine.resume()
            self.console.log("resume requested")
        else:
            self.engine.pause()
            self.console.log("pause requested — the sweep holds after "
                             "the current point")
        self._sync_pause_btn()

    def _sync_pause_btn(self) -> None:
        """Label/tooltip follow the engine's pause request state."""
        paused = (self.engine is not None and self._running
                  and self.engine.pause_requested)
        self.pause_btn.setText("Resume" if paused else "Pause")
        self.pause_btn.setToolTip(
            "Resume the sweep at the next point" if paused
            else "Pause after current point")

    def _on_sweep_paused(self, next_index: int, total: int) -> None:
        """Engine entered the point-boundary hold (queued from worker)."""
        self._paused_msg = (f"SWEEP PAUSED — holding before point "
                            f"{next_index + 1}/{total}")
        self.status_lbl.setText(self._paused_msg)
        self._sync_pause_btn()

    def _on_sweep_resumed(self) -> None:
        self._paused_msg = None
        if self._running:
            self.status_lbl.setText("SWEEP RUNNING")
        self._sync_pause_btn()

    def _abort(self) -> None:
        if self.engine is not None and self._running:
            self.engine.abort()
            self._release_operator_wait("Abort")   # engine may be blocked
            self.console.log("abort requested — finishing current wait")

    def _estop(self) -> None:
        # DIRECT call from the GUI thread — must never be queued
        if self.engine is not None:
            self.engine.estop()        # sets abort + stops all motion
        self._release_operator_wait("E-STOP")      # a stuck dialog must
        self.manager.stop_all_motion()             # never defeat E-STOP
        self.console.log("E-STOP pressed")
        self.status_lbl.setText("E-STOP")

    def _release_operator_wait(self, why: str) -> None:
        """Resolve any pending monitor-only wait with "abort" and close
        its dialog — the ENGINE thread blocked on the ticket must always
        come free when Abort/E-STOP is pressed."""
        ticket, self._wait_ticket = self._wait_ticket, None
        dlg, self._wait_dialog = self._wait_dialog, None
        if ticket is not None:
            ticket.resolve(ABORT_SWEEP)
            self.console.log(f"operator wait released → abort ({why})")
        if dlg is not None:
            dlg.close()

    # ── monitor-only operator wait (engine thread is blocked) ───────────
    def _on_operator_wait(self, ticket: _OperatorWaitTicket) -> None:
        """GUI thread: show the MachWaitDialog for a monitor-only point.
        Window-modal but NON-blocking (dlg.open()); the engine worker is
        the one waiting, on the ticket's Event."""
        if (not self._running or self.engine is None
                or self.engine.abort_requested):
            ticket.resolve(ABORT_SWEEP)    # sweep already going down
            return
        self._wait_ticket = ticket
        dlg = MachWaitDialog(ticket.request, self.config.mach_settle_s,
                             sim=self.manager.sim, parent=self)
        self._wait_dialog = dlg
        dlg.finished.connect(
            lambda _r, d=dlg, t=ticket: self._on_wait_dialog_done(d, t))
        self.console.log(f"operator wait: bring the tunnel to "
                         f"{ticket.request.describe()} — dialog open "
                         "(auto-proceeds once the measurement holds "
                         f"{self.config.mach_settle_s:g} s in tolerance)")
        dlg.open()

    def _on_wait_dialog_done(self, dlg: MachWaitDialog,
                             ticket: _OperatorWaitTicket) -> None:
        self.console.log(f"operator wait: decision '{dlg.decision}' for "
                         f"{ticket.request.describe()}")
        ticket.resolve(dlg.decision)       # no-op if E-STOP resolved first
        if self._wait_dialog is dlg:
            self._wait_dialog = None
            self._wait_ticket = None
        dlg.deleteLater()

    # ── worker callbacks (GUI thread via queued signals) ─────────────────
    def _on_point_state(self, row_index: int, state: str) -> None:
        self.planner.refresh_statuses()
        pts = self._active_points
        if state == "moving" and pts:
            idx = row_index if len(pts) > 1 else 0
            if 0 <= idx < len(pts):
                self.monitors.set_targets(pts[idx].alpha, pts[idx].beta)

    def _on_point_done(self, outcome) -> None:
        """Feed a freshly written point to the live Results polar."""
        if outcome.status == "done" and outcome.path:
            self.monitors.point_done(outcome.path)

    def _on_finished(self, outcomes) -> None:
        done = sum(1 for o in outcomes if o.status == "done")
        failed = sum(1 for o in outcomes if o.status == "failed")
        skipped = sum(1 for o in outcomes if o.status == "skipped")
        self.console.log(f"sweep finished: {done} done, {failed} failed, "
                         f"{skipped} skipped")
        self.planner.refresh_statuses()

    def _on_thread_finished(self) -> None:
        self._running = False
        self._paused_msg = None
        if self._runner is not None:
            self._runner.deleteLater()
        self._runner = None
        self._thread = None
        self.monitors.set_targets(None, None)
        self._update_ui_state()

    # ── recorder factory ─────────────────────────────────────────────────
    def _make_recorder(self) -> Hdf5Recorder:
        """Build a recorder honouring the current data root, config name,
        filename template and output format. Called again on every sweep
        launch (see :meth:`_launch`), so Measurement Setup edits — incl.
        the file-format pulldown — take effect for the NEXT sweep without
        a restart."""
        fmt = str(getattr(self.config, "output_format", "h5") or "h5")
        try:
            return Hdf5Recorder(self.config.data_root,
                                self.config.config_name,
                                self.config.filename_template,
                                output_format=fmt)
        except (ImportError, ValueError) as exc:   # missing dep / bad fmt
            log.warning("%s output unavailable: %s — falling back to "
                        "HDF5", fmt, exc)
            if hasattr(self, "console"):
                self.console.log(f"{fmt} output unavailable ({exc}) — "
                                 "falling back to HDF5 (.h5)")
            return Hdf5Recorder(self.config.data_root,
                                self.config.config_name,
                                self.config.filename_template)

    # ── device-config bundling ───────────────────────────────────────────
    def _snapshot_device_configs(self) -> None:
        """Pull EVERY registered device's driver config into the bundle so
        Save Config persists all of them, not just Freestream's own."""
        for dev_id, dev in self.manager.devices.items():
            snap = getattr(dev, "config_dict", None)
            if callable(snap):
                try:
                    self.config.device_configs[dev_id] = snap()
                except Exception as exc:               # noqa: BLE001
                    self.console.log(f"config snapshot {dev_id} failed: "
                                     f"{exc}")

    def _apply_device_configs(self) -> None:
        """Push saved driver configs back into the live adapters."""
        for dev_id, dev in self.manager.devices.items():
            data = self.config.device_configs.get(dev_id)
            apply = getattr(dev, "apply_config_dict", None)
            if data and callable(apply):
                try:
                    apply(data)
                except Exception as exc:               # noqa: BLE001
                    self.console.log(f"apply config {dev_id} failed: {exc}")

    def _open_device_settings(self, dev_id: str) -> None:
        """Clicking a device card opens the FULL device configuration GUI
        (settings + channels + axis calibration, mirroring the standalone
        device app)."""
        dev = self.manager.devices.get(dev_id)
        if dev is None:
            return
        if not callable(getattr(dev, "config_dict", None)):
            self.console.log(f"{dev_id}: no configurable driver config")
            return
        try:
            from .device_config import DeviceConfigDialog
            dlg = DeviceConfigDialog(
                dev, self,
                # live callbacks: connect/disconnect + Set-as-Defaults act
                # IMMEDIATELY (no dialog close needed) and stay consistent
                # with the rail/console/_connected bookkeeping
                on_connect=lambda d=dev_id: self._connect_device(d),
                on_disconnect=lambda d=dev_id: self._disconnect_device(d),
                on_save_defaults=lambda d=dev_id:
                    self._save_device_defaults(d))
            accepted = bool(dlg.exec())
            changed = accepted or dlg.applied          # Apply w/o OK counts
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.warning(self, f"{dev_id} settings", str(exc))
            self.console.log(f"{dev_id} settings failed: {exc}")
            return
        if changed:
            self.config.device_configs[dev_id] = dev.config_dict()
            self.console.log(f"{dev_id} configuration updated "
                             "(communication/sampling changes apply on "
                             "next connect)")
            self.rail.poll()

    def _open_balance_device(self) -> None:
        """Forces page → "Balance device…": open the balance device's
        config dialog (the StrainBook panel — the single .vol/fit/layout
        editor the Forces readout inherits from)."""
        bal = self.manager.by_role("balance")
        dev_id = getattr(bal, "id", None) if bal is not None else None
        if dev_id and dev_id in self.manager.devices:
            self._open_device_settings(dev_id)
        else:
            self.console.log("no balance device in the active set — "
                             "nothing to configure")

    # ── per-device connect (single-device bring-up) ──────────────────────
    def _connect_device(self, dev_id: str) -> None:
        dev = self.manager.devices.get(dev_id)
        if dev is None or dev.connected:
            return
        try:
            setter = getattr(dev, "set_sample_rate", None)
            if callable(setter):                       # unified rate
                setter(float(self.config.sample_rate_hz))
            dev.connect()
            if isinstance(dev, Streaming):
                dev.start()
            self.console.log(f"{dev_id} connected"
                             + (" — stream started"
                                if isinstance(dev, Streaming) else ""))
        except Exception as exc:                       # noqa: BLE001
            self.console.log(f"connect {dev_id} FAILED: {exc}")
        if all(d.connected for d in self.manager.devices.values()):
            self._connected = True                     # full set is up
        self.rail.poll()
        self._update_ui_state()

    def _disconnect_device(self, dev_id: str) -> None:
        if self._running:
            self.console.log("cannot disconnect a device while a sweep "
                             "runs")
            return
        dev = self.manager.devices.get(dev_id)
        if dev is None or not dev.connected:
            return
        try:
            if isinstance(dev, Streaming):
                dev.stop()
            dev.disconnect()
            self.console.log(f"{dev_id} disconnected")
        except Exception as exc:                       # noqa: BLE001
            self.console.log(f"disconnect {dev_id} failed: {exc}")
        if not any(d.connected for d in self.manager.devices.values()):
            self._connected = False
        self.rail.poll()
        self._update_ui_state()

    def _save_device_defaults(self, dev_id: str) -> None:
        """Device dialog → "Set as Defaults": snapshot THIS adapter's
        config_dict() into the bundle, then persist the whole startup-
        defaults file (reuses :meth:`_save_defaults`, which re-snapshots
        every device — the point is that this device's edits land NOW)."""
        dev = self.manager.devices.get(dev_id)
        if dev is not None and callable(getattr(dev, "config_dict", None)):
            try:
                self.config.device_configs[dev_id] = dev.config_dict()
            except Exception as exc:                   # noqa: BLE001
                self.console.log(f"config snapshot {dev_id} failed: {exc}")
        self._save_defaults()

    # ── config menu ──────────────────────────────────────────────────────
    def _save_defaults(self) -> None:
        """Snapshot EVERYTHING (measurement settings + every device's
        driver config: rates, ranges, resolutions, directories) to the
        startup-defaults file — auto-loaded on the next launch. This is
        SEPARATE from named Save/Load Config files."""
        from ..config import defaults_path
        self._snapshot_device_configs()
        p = defaults_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        self.config.save(p)
        self.console.log(
            f"current settings stored as startup defaults → {p}  "
            f"({len(self.config.device_configs)} device configs bundled)")

    def _save_config(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Config", "freestream_config.json",
            "Config (*.json)")
        if not path:
            return
        self._snapshot_device_configs()                # save EVERY config
        self.config.save(path)
        self.console.log(f"config saved → {path}  "
                         f"({len(self.config.device_configs)} device configs "
                         "bundled)")

    def _load_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load Config", "", "Config (*.json)")
        if not path:
            return
        try:
            self.config = FreestreamConfig.load(path)
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.warning(self, "Load Config", str(exc))
            return
        self.recorder = self._make_recorder()
        if not self._connected:
            if (self.config.mode == DeviceManager.CUSTOM
                    and self.config.custom_devices):
                # rebuild the exact saved subset WITHOUT re-opening the
                # picker (going through the combo would prompt)
                try:
                    self._build_and_adopt_custom(self.config.custom_devices)
                except Exception as exc:               # noqa: BLE001
                    self.console.log(f"custom set from config failed: {exc}")
            elif self.config.mode != self.manager.mode:
                self.mode_combo.setCurrentText(self.config.mode)  # rebuilds
        if (self.config.sim != self.manager.sim
                and not self._connected):              # follow saved SIM/LIVE
            self.sim_combo.setCurrentText(
                "SIM" if self.config.sim else "LIVE")  # rebuilds mgr
        self._apply_device_configs()                   # restore driver cfgs
        self.planner.set_config(self.config)
        self.console.log(f"config loaded ← {path} "
                         f"(config_name={self.config.config_name}, "
                         f"{len(self.config.device_configs)} device configs)")

    def _on_run_applied(self) -> None:
        """A run sheet was applied in the planner: it mutated the shared
        config (samples/rate + test-info + reference dims). Rebuild the
        recorder and re-push the sample rate so the change takes effect."""
        self.recorder = self._make_recorder()
        self._push_sample_rate()      # live-capable devices apply at once
        self.console.log(
            f"run sheet applied → test={self.config.test_name or '—'}, "
            f"model={self.config.model_name or '—'}, "
            f"samples={self.config.samples}, "
            f"rate={self.config.sample_rate_hz:g} Hz, "
            f"Sref={self.config.Sref:g} cref={self.config.cref:g}")

    def _open_setup(self) -> None:
        dlg = MeasurementSetupDialog(self.config,
                                     list(self.manager.devices), self)
        if dlg.exec():
            dlg.apply_to(self.config)
            # the (possibly new) speed unit re-skins the planner's
            # speed row / table header / indicator symbol
            self.planner.set_speed_unit(self.config.speed_unit)
            self.recorder = self._make_recorder()
            # Push even while connected: adapters that support a live
            # rate change (NI DAQ — cheap DAQmx task restart) apply it on
            # the spot; the DaqX devices stage it for the next connect.
            self._push_sample_rate()
            if self._connected:
                self.console.log("sample rate pushed — live-capable "
                                 "devices restarted; DaqX devices apply "
                                 "at the next connect")
            # (the balance .vol is DEVICE-owned: the Forces page inherits
            # it from the StrainBook panel each tick — nothing to push here)
            outputs = "." + str(getattr(self.config, "output_format", "h5")
                                or "h5")
            self.console.log(
                f"measurement setup updated (config_name="
                f"{self.config.config_name}, data_root="
                f"{self.config.data_root}, sample rate="
                f"{self.config.sample_rate_hz:g} Hz, output={outputs})")
            if getattr(dlg, "defaults_requested", False):
                self._save_defaults()

    # ── shutdown ─────────────────────────────────────────────────────────
    def closeEvent(self, event) -> None:               # noqa: N802
        if self.engine is not None:
            self.engine.abort()
        self._release_operator_wait("window close")    # unblock the worker
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(5000)
        for panel in (self.rail, self.monitors, self.planner):
            panel.shutdown()
        try:
            self.manager.disconnect_all()
        except Exception:                              # noqa: BLE001
            pass
        super().closeEvent(event)
