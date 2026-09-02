"""Main window for the South LSWT traverse app.

Layout: connection bar → three axis cards (X | Y | Z) + E-STOP column →
position time-history → status log. Deliberately simpler than the SWT
traverse app: the SmartSteps position themselves, and referencing is
one button — jog to the reference spot, "Set home here", done. The
soft-limit spinboxes on each card ARE the "software limitations":
edited live, enforced host-side, persisted with Set as Defaults.
"""

from __future__ import annotations

import logging
import webbrowser
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction
from PyQt6.QtWidgets import (
    QBoxLayout, QCheckBox, QDoubleSpinBox, QGridLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox,
    QPlainTextEdit, QPushButton, QSizePolicy, QStatusBar, QVBoxLayout,
    QWidget,
)

from lswt_traverse import about, theme
from lswt_traverse.config import TraverseConfig, defaults_path
from lswt_traverse.device import AXES, LswtTraverseDrive

log = logging.getLogger(__name__)

theme.apply_pyqtgraph_theme()

_AXIS_TITLES = {"X": "X — Axial", "Y": "Y — Lateral", "Z": "Z — Vertical"}
_AXIS_COLORS = {"X": "#4fc1ff", "Y": "#dcdcaa", "Z": "#c586c0"}


def _unbind_width(*labels: QLabel) -> None:
    """Stop live-text labels from binding layout minimums.

    Readouts change width with their values ("+3.000", "FAULT: chain
    timeout", "HOME SET"), and a QLabel's minimum hint is its text
    width — so every value change would breathe the axis-card minimums
    and with them the reflow thresholds. Ignored horizontal policy
    makes the labels take whatever the grid gives them instead.
    """
    for lbl in labels:
        pol = lbl.sizePolicy()
        pol.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        lbl.setSizePolicy(pol)


def _floor_spin_width(floor: int, *spins: QDoubleSpinBox) -> None:
    """Let wide-range spinboxes shrink below their value-width hint.

    A spinbox's layout minimum is the width of its longest representable
    value — with the ±1000" ranges here that is ~170 px per box, and
    three boxes per card jailed each card at ~730 px (the whole window
    at ~2340 px). Ignored horizontal policy drops the hint from the
    layout math; the explicit minimum keeps the boxes usable (typical
    values like -12.00" fit comfortably).
    """
    for sp in spins:
        sp.setMinimumWidth(floor)
        pol = sp.sizePolicy()
        pol.setHorizontalPolicy(QSizePolicy.Policy.Ignored)
        sp.setSizePolicy(pol)


class _ReflowRow(QWidget):
    """Two cluster widgets side-by-side (wide) or stacked (narrow) — the
    SWT traverse app's pattern, reused for the Connection bar so its
    seven controls stop dictating the window's minimum width."""

    _MARGIN = 24

    def __init__(self, first: QWidget, second: QWidget, parent=None):
        super().__init__(parent)
        self._first, self._second = first, second
        self._lay = QBoxLayout(QBoxLayout.Direction.LeftToRight, self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(8)
        self._lay.addWidget(first)
        self._lay.addWidget(second)
        self._horizontal = True

    @staticmethod
    def _minw(w: QWidget) -> int:
        return max(w.minimumSizeHint().width(), w.minimumWidth(), 1)

    @property
    def threshold(self) -> int:
        return self._minw(self._first) + self._minw(self._second) + self._MARGIN

    @property
    def stacked(self) -> bool:
        return not self._horizontal

    def minimumSizeHint(self) -> QSize:                # noqa: N802
        hint = super().minimumSizeHint()
        return QSize(max(self._minw(self._first), self._minw(self._second)),
                     hint.height())

    def reflow(self, width: int) -> None:
        horizontal = width >= self.threshold
        if horizontal == self._horizontal:
            return
        self._horizontal = horizontal
        self._lay.setDirection(
            QBoxLayout.Direction.LeftToRight if horizontal
            else QBoxLayout.Direction.TopToBottom)

    def resizeEvent(self, event):                      # noqa: N802
        super().resizeEvent(event)
        self.reflow(self.width())


class _ReflowCards(QWidget):
    """Holds the three axis cards + the E-STOP box and REFLOWS between
    three arrangements as the window is resized (the SWT traverse app's
    pattern):

        wide    1 x 4   X | Y | Z | E-STOP       (``columns == 4``)
        medium  2 x 2   X  Y  /  Z  E-STOP       (``columns == 2``)
        narrow  stacked                           (``columns == 1``)

    One :class:`QGridLayout` is repopulated in place — the boxes stay
    children of this container, nothing is reparented, so every control
    (jog, Move, STOP, Set home, E-STOP) stays wired in every
    arrangement.

    ``minimumSizeHint`` reports a ONE-card width so the window can be
    shrunk BELOW the wide row's own minimum — that shrink is exactly
    what drives the reflow (without it, the 1x4 row's minimum would hold
    the window open and the narrower modes could never trigger).
    """

    _SPACING = 8

    def __init__(self, cards, side: QWidget, parent=None):
        super().__init__(parent)
        self._cards = list(cards)              # X, Y, Z axis cards
        self._side = side                      # E-STOP box
        self._widgets = self._cards + [side]
        self._lay = QGridLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(self._SPACING)
        self._columns = 0
        self.reflow(10_000)                    # start in the wide layout

    @staticmethod
    def _minw(w: QWidget) -> int:
        return max(w.minimumSizeHint().width(), w.minimumWidth(), 1)

    @property
    def columns(self) -> int:
        """Current arrangement: 4 (row), 2 (2x2) or 1 (stacked)."""
        return self._columns

    @property
    def wide_threshold(self) -> int:
        """Width needed for the 1x4 row (sum of minimums + spacing)."""
        return (sum(self._minw(w) for w in self._widgets)
                + 3 * self._SPACING + 16)

    @property
    def two_col_threshold(self) -> int:
        """Width needed for the 2x2 grid (widest row-pair of minimums)."""
        pairs = (self._minw(self._cards[0]) + self._minw(self._cards[1]),
                 self._minw(self._cards[2]) + self._minw(self._side))
        return max(pairs) + self._SPACING + 16

    def minimumSizeHint(self) -> QSize:                # noqa: N802
        hint = super().minimumSizeHint()
        return QSize(max(self._minw(w) for w in self._widgets),
                     hint.height())

    def reflow(self, width: int) -> None:
        """Pick the arrangement for *width* (also the tests' entry point)."""
        cols = (4 if width >= self.wide_threshold else
                2 if width >= self.two_col_threshold else 1)
        if cols == self._columns:
            return
        self._columns = cols
        while self._lay.count():
            self._lay.takeAt(0)
        for c in range(4):
            self._lay.setColumnStretch(c, 0)
        for i, w in enumerate(self._widgets):
            r, c = divmod(i, cols)
            self._lay.addWidget(w, r, c)
        if cols == 4:
            # keep the wide row's visual weights: axis cards 3, E-STOP 1
            for c in range(3):
                self._lay.setColumnStretch(c, 3)
            self._lay.setColumnStretch(3, 1)
        else:
            for c in range(cols):
                self._lay.setColumnStretch(c, 1)

    def resizeEvent(self, event):                      # noqa: N802
        super().resizeEvent(event)
        self.reflow(self.width())


class _AxisCard(QGroupBox):
    """Readout + motion controls for one traverse axis."""

    moveRequested = pyqtSignal(str, float)
    stopRequested = pyqtSignal(str)
    homeRequested = pyqtSignal(str)
    jogStarted = pyqtSignal(str, bool)
    jogStopped = pyqtSignal(str)
    limitsEdited = pyqtSignal(str, float, float)

    #: usable floor for one axis card — the reflow container derives its
    #: 4-column / 2-column / stacked thresholds from the cards' minimums.
    MIN_WIDTH = 280

    def __init__(self, name: str, parent=None):
        super().__init__(_AXIS_TITLES.get(name, name), parent)
        self.axis_name = name
        self.setMinimumWidth(self.MIN_WIDTH)
        g = QGridLayout(self)

        chip = QLabel()
        chip.setFixedSize(10, 10)
        chip.setStyleSheet(f"background-color: {_AXIS_COLORS[name]}; "
                           f"border-radius: 5px;")
        g.addWidget(chip, 0, 0)
        self.big_lbl = QLabel("--")
        self.big_lbl.setStyleSheet(
            "font-family: 'Segoe UI'; font-size: 24pt; font-weight: 600; "
            f"color: {theme.TEXT};")
        g.addWidget(self.big_lbl, 0, 1, 1, 2)
        unit = QLabel("in")
        unit.setObjectName("dim")
        g.addWidget(unit, 0, 3)

        self.state_lbl = QLabel("idle")
        self.state_lbl.setProperty("mono", "true")
        g.addWidget(self.state_lbl, 1, 1, 1, 2)
        self.ref_lbl = QLabel("NO HOME")
        self.ref_lbl.setStyleSheet(f"color: {theme.WARNING}; "
                                   "font-weight: bold;")
        self.ref_lbl.setToolTip(
            "The drive reads 0.000 wherever it woke up. Jog to the "
            "reference spot, then Set home — absolute moves stay "
            "locked until then.")
        g.addWidget(self.ref_lbl, 1, 3)

        # ── jog ──
        self.jog_neg = QPushButton("◀ jog")
        self.jog_pos = QPushButton("jog ▶")
        for btn, positive in ((self.jog_neg, False), (self.jog_pos, True)):
            btn.setAutoRepeat(False)
            btn.pressed.connect(
                lambda p=positive: self.jogStarted.emit(self.axis_name, p))
            btn.released.connect(
                lambda: self.jogStopped.emit(self.axis_name))
        g.addWidget(self.jog_neg, 2, 0, 1, 2)
        g.addWidget(self.jog_pos, 2, 2, 1, 2)

        # ── absolute move ──
        g.addWidget(QLabel("Target"), 3, 0)
        self.target = QDoubleSpinBox()
        self.target.setDecimals(3)
        self.target.setRange(-1000.0, 1000.0)
        self.target.setSingleStep(0.1)
        self.target.setSuffix('"')
        g.addWidget(self.target, 3, 1)
        self.move_btn = QPushButton("Move")
        self.move_btn.clicked.connect(
            lambda: self.moveRequested.emit(self.axis_name,
                                            self.target.value()))
        g.addWidget(self.move_btn, 3, 2)
        self.stop_btn = QPushButton("STOP")
        self.stop_btn.setObjectName("danger")
        self.stop_btn.clicked.connect(
            lambda: self.stopRequested.emit(self.axis_name))
        g.addWidget(self.stop_btn, 3, 3)

        # ── referencing + soft limits ──
        self.home_btn = QPushButton("Set home here")
        self.home_btn.setToolTip(
            "Declare the CURRENT position home (wire SP). The soft "
            "limits below then gate every move, host-side.")
        self.home_btn.clicked.connect(
            lambda: self.homeRequested.emit(self.axis_name))
        g.addWidget(self.home_btn, 4, 0, 1, 2)

        g.addWidget(QLabel("Limits"), 4, 2,
                    alignment=Qt.AlignmentFlag.AlignRight)
        lim = QHBoxLayout()
        self.min_spin = QDoubleSpinBox()
        self.max_spin = QDoubleSpinBox()
        for sp in (self.min_spin, self.max_spin):
            sp.setDecimals(2)
            sp.setRange(-1000.0, 1000.0)
            sp.setSuffix('"')
            sp.editingFinished.connect(self._emit_limits)
        lim.addWidget(self.min_spin)
        lim.addWidget(self.max_spin)
        g.addLayout(lim, 4, 3)

        # live-value labels must not set the card's minimum (and thereby
        # the reflow thresholds), and the ±1000" spin ranges must not
        # either — the buttons row is the usable floor
        _unbind_width(self.big_lbl, self.state_lbl, self.ref_lbl)
        _floor_spin_width(72, self.target)
        _floor_spin_width(64, self.min_spin, self.max_spin)

    def _emit_limits(self):
        self.limitsEdited.emit(self.axis_name, self.min_spin.value(),
                               self.max_spin.value())

    def set_limits(self, lo: float, hi: float):
        for sp, v in ((self.min_spin, lo), (self.max_spin, hi)):
            if abs(sp.value() - v) > 1e-9:
                sp.blockSignals(True)
                sp.setValue(v)
                sp.blockSignals(False)

    def set_state(self, st: dict):
        self.big_lbl.setText(f"{st['position']:+.3f}")
        text = st["state"]
        if st["fault"]:
            text = f"FAULT: {st['fault']}"
        color = (theme.ERROR if st["fault"] or "limit" in text
                 or "TIMEOUT" in text or "KILLED" in text
                 else theme.ACCENT_LIGHT if st["moving"] else theme.TEXT_DIM)
        self.state_lbl.setText(text)
        self.state_lbl.setStyleSheet(f"color: {color};")
        if st["referenced"]:
            self.ref_lbl.setText("HOME SET")
            self.ref_lbl.setStyleSheet(
                f"color: {theme.SUCCESS}; font-weight: bold;")
        else:
            self.ref_lbl.setText("NO HOME")
            self.ref_lbl.setStyleSheet(
                f"color: {theme.WARNING}; font-weight: bold;")
        self.move_btn.setEnabled(st["referenced"] and not st["fault"])

    def set_connected(self, connected: bool):
        for w in (self.jog_neg, self.jog_pos, self.move_btn,
                  self.stop_btn, self.home_btn):
            w.setEnabled(connected)
        if not connected:
            self.big_lbl.setText("--")
            self.state_lbl.setText("idle")


class TraverseMainWindow(QMainWindow):
    statusSignal = pyqtSignal(str)

    def __init__(self, config: TraverseConfig | None = None):
        super().__init__()
        self.config = config or TraverseConfig.load_defaults()
        self.device = LswtTraverseDrive(self.config)
        self.device.on_status = self.statusSignal.emit
        self.statusSignal.connect(self._log_status)

        self.setWindowTitle(f"{about.APP_NAME}  v{about.__version__}")
        self.setStyleSheet(theme.get_stylesheet())
        self._build_ui()
        # opens in the 2x2 card layout with a single-row Connection bar;
        # widen past the cards' wide threshold for the classic 1x4 row,
        # or shrink to ~700 and everything reflows/stacks
        self.resize(1140, 820)
        theme.install_wheel_guard(self)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_ui)
        self._timer.start(150)
        self._last_connected = False

    # ── UI construction ──────────────────────────────────────────────────
    def _build_ui(self):
        central = QWidget()
        root = QVBoxLayout(central)
        self.setCentralWidget(central)

        # connection bar — two clusters (port/sim | lamp/actions) that sit
        # side-by-side when there is room and stack when the window narrows
        left = QWidget()
        ll = QHBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.setSpacing(8)
        ll.addWidget(QLabel("Port"))
        self.port_edit = QLineEdit(self.config.port)
        self.port_edit.setFixedWidth(90)
        self.port_edit.setToolTip("COM port of the RS-232 daisy chain "
                                  "(9600 8N1, XON/XOFF)")
        ll.addWidget(self.port_edit)
        self.sim = QCheckBox("Simulation")
        self.sim.setChecked(self.config.force_sim)
        ll.addWidget(self.sim)
        ll.addStretch(1)

        right = QWidget()
        rl = QHBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.setSpacing(8)
        rl.addStretch(1)
        self.lamp = QLabel("DISCONNECTED")
        self.lamp.setStyleSheet(f"color: {theme.TEXT_DIM}; "
                                "font-weight: bold;")
        rl.addWidget(self.lamp)
        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("primary")
        self.connect_btn.clicked.connect(self._handle_connect)
        rl.addWidget(self.connect_btn)
        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setEnabled(False)
        self.disconnect_btn.clicked.connect(self._handle_disconnect)
        rl.addWidget(self.disconnect_btn)
        self.defaults_btn = QPushButton("Set as Defaults")
        self.defaults_btn.setToolTip(
            "Persist port, limits, speeds — auto-loads at every launch")
        self.defaults_btn.clicked.connect(self.save_defaults)
        rl.addWidget(self.defaults_btn)

        self.conn_row = _ReflowRow(left, right)
        root.addWidget(self.conn_row)

        # axis cards + E-stop
        self.cards = {}
        for name in AXES:
            card = _AxisCard(name)
            card.moveRequested.connect(self._move)
            card.stopRequested.connect(self._stop_axis)
            card.homeRequested.connect(self._set_home)
            card.jogStarted.connect(self._jog_start)
            card.jogStopped.connect(self._jog_stop)
            card.limitsEdited.connect(self._limits_edited)
            cfg = self.config.axis(name)
            card.set_limits(cfg.min_in, cfg.max_in)
            card.set_connected(False)
            self.cards[name] = card

        side = self.side_box = QWidget()
        estop_col = QVBoxLayout(side)
        estop_col.setContentsMargins(0, 0, 0, 0)
        self.estop_btn = QPushButton("E-STOP")
        self.estop_btn.setObjectName("danger")
        self.estop_btn.setMinimumHeight(80)
        self.estop_btn.setEnabled(False)
        self.estop_btn.setToolTip("Broadcast decel-stop (S) to the "
                                  "whole chain")
        self.estop_btn.clicked.connect(self._estop)
        estop_col.addWidget(self.estop_btn)
        estop_col.addStretch(1)

        # the four boxes reflow (1x4 ↔ 2x2 ↔ stacked) with window width
        self.cards_container = _ReflowCards(
            [self.cards[n] for n in AXES], side)
        root.addWidget(self.cards_container)

        # position history
        self.plot = pg.PlotWidget()
        self.plot.setMinimumHeight(180)
        pi = self.plot.getPlotItem()
        pi.setLabel("left", "position  (in)")
        pi.setLabel("bottom", "time  (s)")
        pi.addLegend(offset=(8, 8), labelTextColor=theme.TEXT)
        self._curves = {
            name: pi.plot([], [], name=name, antialias=False,
                          pen=pg.mkPen(_AXIS_COLORS[name], width=1))
            for name in AXES}
        # clear-plot: time-watermark context menu, the house convention
        self._plot_t0 = 0.0
        self.plot.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self.plot.customContextMenuRequested.connect(self._plot_menu)
        root.addWidget(self.plot, 1)

        # status log
        self.log_box = QPlainTextEdit()
        self.log_box.setReadOnly(True)
        self.log_box.setMaximumBlockCount(500)
        self.log_box.setFixedHeight(110)
        root.addWidget(self.log_box)

        self.setStatusBar(QStatusBar())

        # menu
        help_menu = self.menuBar().addMenu("&Help")
        manual = QAction("SmartStep23 manual (PDF)", self)
        manual.triggered.connect(self._open_manual)
        help_menu.addAction(manual)
        about_act = QAction("About", self)
        about_act.triggered.connect(self._about)
        help_menu.addAction(about_act)

    # ── actions ──────────────────────────────────────────────────────────
    def _handle_connect(self):
        self.config.port = self.port_edit.text().strip() or "COM1"
        self.config.force_sim = self.sim.isChecked()
        try:
            self.device.connect()
        except (ConnectionError, OSError) as exc:
            QMessageBox.warning(self, "Connect failed", str(exc))
            return
        self._set_connected_ui(True)

    def _handle_disconnect(self):
        self.device.disconnect()
        self._set_connected_ui(False)

    def _guard(self, fn, *args):
        try:
            fn(*args)
        except (ValueError, RuntimeError) as exc:
            self.statusSignal.emit(str(exc))

    def _move(self, name: str, value: float):
        self._guard(self.device.move_to, *(
            (value, None, None) if name == "X" else
            (None, value, None) if name == "Y" else (None, None, value)))

    def _stop_axis(self, name: str):
        self._guard(self.device.stop_axis, name)

    def _set_home(self, name: str):
        self._guard(self.device.set_home, name)

    def _jog_start(self, name: str, positive: bool):
        self._guard(self.device.jog, name, positive)

    def _jog_stop(self, name: str):
        self._guard(self.device.stop_axis, name)

    def _estop(self):
        self._guard(self.device.stop_all)

    def _limits_edited(self, name: str, lo: float, hi: float):
        cfg = self.config.axis(name)
        if hi <= lo:
            self.statusSignal.emit(
                f"{name}: max limit must exceed min — edit ignored")
            self.cards[name].set_limits(cfg.min_in, cfg.max_in)
            return
        cfg.min_in, cfg.max_in = lo, hi
        self.statusSignal.emit(
            f"{name}: soft limits set to [{lo:+.2f}, {hi:+.2f}]\" "
            f"(Set as Defaults to keep them)")

    def save_defaults(self):
        self.config.port = self.port_edit.text().strip() or "COM1"
        path = defaults_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.config.save(path)
        except OSError as exc:
            self.statusSignal.emit(f"Defaults save FAILED: {exc}")
            return
        self.statusSignal.emit(f"Defaults saved — auto-loads at every "
                               f"launch ({path})")

    def _plot_menu(self, pos):
        from PyQt6.QtWidgets import QMenu
        menu = QMenu(self)
        clear = menu.addAction("Clear plot")
        if menu.exec(self.plot.mapToGlobal(pos)) is clear:
            latest = self.device.ring.latest()
            self._plot_t0 = latest["t"] if latest else 0.0

    def _open_manual(self):
        pdf = Path(__file__).resolve().parents[1] / \
            "SmartStep23_User's_Manual.pdf"
        if pdf.is_file():
            webbrowser.open(pdf.as_uri())

    def _about(self):
        QMessageBox.about(self, "About",
                          f"{about.APP_NAME}\nv{about.__version__}\n\n"
                          f"{about.SUMMARY}")

    # ── refresh ──────────────────────────────────────────────────────────
    def _set_connected_ui(self, connected: bool):
        self.connect_btn.setEnabled(not connected)
        self.disconnect_btn.setEnabled(connected)
        self.port_edit.setEnabled(not connected)
        self.sim.setEnabled(not connected)
        self.estop_btn.setEnabled(connected)
        for card in self.cards.values():
            card.set_connected(connected)
        if not connected:
            self.lamp.setText("DISCONNECTED")
            self.lamp.setStyleSheet(f"color: {theme.TEXT_DIM}; "
                                    "font-weight: bold;")
        elif self.device.sim_mode:
            self.lamp.setText("SIMULATION")
            self.lamp.setStyleSheet(f"color: {theme.WARNING}; "
                                    "font-weight: bold;")
        else:
            self.lamp.setText("LIVE")
            self.lamp.setStyleSheet(f"color: {theme.SUCCESS}; "
                                    "font-weight: bold;")

    def _refresh_ui(self):
        connected = self.device.connected
        if connected != self._last_connected:
            self._last_connected = connected
            self._set_connected_ui(connected)
        if not connected:
            return
        state = self.device.state()
        for name, card in self.cards.items():
            card.set_state(state[name])

        window = self.config.plot_window_s
        n = int(window / max(self.config.poll_s, 0.01) * 1.1) + 2
        data = self.device.ring.tail(n)
        t = data["t"]
        if t.size >= 2:
            keep = t >= max(t[-1] - window, self._plot_t0)
            for name, curve in self._curves.items():
                curve.setData(t[keep], data[name][keep])

    def _log_status(self, msg: str):
        self.log_box.appendPlainText(msg)
        self.statusBar().showMessage(msg, 5000)

    def closeEvent(self, ev):                          # noqa: N802
        self.device.disconnect()
        super().closeEvent(ev)
