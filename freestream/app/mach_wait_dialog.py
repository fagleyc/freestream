"""MachWaitDialog — operator-in-the-loop tunnel condition (monitor-only).

The Red Lion currently REJECTS Block2 writes (Crimson fix pending), so
Freestream never commands fan RPM: for each mach (or run-sheet ``rpm``
override) point the sweep engine raises an
:class:`freestream.sweep.OperatorWaitRequest` and this dialog asks the
OPERATOR to bring the tunnel to the target on the console. It polls
``request.measure()`` at ~4 Hz, shows target vs LIVE measured value with
an explicit IN/OUT-OF-TOLERANCE text state (never color alone), and
AUTO-PROCEEDS once the measurement holds inside the tolerance for
``settle_s`` continuously. No timeout — the operator is present.

Buttons: "Proceed anyway" | "Skip point" | "Abort sweep"; closing the
dialog (X / Esc) counts as abort — the safe default.

Threading: shown window-modal via ``QDialog.open()`` (NON-blocking — the
Qt event loop keeps running); it is the ENGINE worker thread that blocks,
on a ``threading.Event`` the main window resolves from this dialog's
``finished`` signal (or from Abort/E-STOP, which must always release it).

SIM: pressures don't track the console, so the dialog shows a note and
auto-proceeds after 1 s to keep sim sweeps hands-free.
"""

from __future__ import annotations

import math
import time
from typing import Optional

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QDialog, QGridLayout, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout)

from .. import theme
from ..sweep import ABORT_SWEEP, PROCEED, SKIP_POINT, OperatorWaitRequest

POLL_MS = 250                      # ~4 Hz live update
SIM_AUTO_PROCEED_MS = 1000         # sim: hands-free after 1 s

_BIG = ('font-family: "Consolas", monospace; font-size: 30pt; '
        'font-weight: bold;')


class MachWaitDialog(QDialog):
    """One monitor-only operator wait; read the ``decision`` attribute
    ("proceed" | "skip" | "abort") after ``finished`` fires."""

    def __init__(self, request: OperatorWaitRequest, settle_s: float,
                 sim: bool = False, parent=None):
        super().__init__(parent)
        self.request = request
        self.settle_s = max(float(settle_s), 0.0)
        self.sim = bool(sim)
        self.decision = ABORT_SWEEP        # X / Esc == abort (safe default)
        self._decided = False
        self._hold_t0: Optional[float] = None

        unit = "RPM" if request.is_rpm else "Mach"
        self.setWindowTitle(f"Set Tunnel Condition — {request.describe()}")
        self.setMinimumWidth(520)
        self.setStyleSheet(theme.get_stylesheet())

        lay = QVBoxLayout(self)
        lay.setSpacing(10)

        note = QLabel("MONITOR-ONLY — tunnel control disabled (Red Lion "
                      "Block2 writes rejected). Set the fan on the tunnel "
                      f"console until the measured {unit} holds at the "
                      "target.")
        note.setWordWrap(True)
        note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(note)

        grid = QGridLayout()
        grid.setHorizontalSpacing(30)
        for col, caption in enumerate((f"Target {unit}",
                                       f"Measured {unit}")):
            cap = QLabel(caption)
            cap.setStyleSheet(f"color: {theme.TEXT_DIM};")
            grid.addWidget(cap, 0, col,
                           alignment=Qt.AlignmentFlag.AlignHCenter)
        self.target_lbl = QLabel(self._fmt(
            request.target_rpm if request.is_rpm else request.target_mach))
        self.target_lbl.setStyleSheet(_BIG + f"color: {theme.ACCENT_LIGHT};")
        grid.addWidget(self.target_lbl, 1, 0,
                       alignment=Qt.AlignmentFlag.AlignHCenter)
        self.measured_lbl = QLabel("—")
        self.measured_lbl.setStyleSheet(_BIG + f"color: {theme.TEXT};")
        grid.addWidget(self.measured_lbl, 1, 1,
                       alignment=Qt.AlignmentFlag.AlignHCenter)
        lay.addLayout(grid)

        self.delta_lbl = QLabel("Δ — waiting for measurement…")
        self.delta_lbl.setStyleSheet(
            f'font-family: "Consolas", monospace; font-size: 12pt; '
            f"color: {theme.WARNING};")
        lay.addWidget(self.delta_lbl,
                      alignment=Qt.AlignmentFlag.AlignHCenter)

        # secondary readback: fan RPM for mach points; measured Mach for
        # rpm-override points
        self.readback_lbl = QLabel("")
        self.readback_lbl.setStyleSheet(
            f'font-family: "Consolas", monospace; color: {theme.TEXT_DIM};')
        lay.addWidget(self.readback_lbl,
                      alignment=Qt.AlignmentFlag.AlignHCenter)

        self.hold_lbl = QLabel("waiting for the tunnel to reach the "
                               "target…")
        self.hold_lbl.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(self.hold_lbl,
                      alignment=Qt.AlignmentFlag.AlignHCenter)

        self.sim_lbl = QLabel("SIM — pressures won't track the console; "
                              "auto-proceeding in 1 s")
        self.sim_lbl.setStyleSheet(
            f"color: {theme.WARNING}; font-weight: bold;")
        self.sim_lbl.setVisible(self.sim)
        lay.addWidget(self.sim_lbl,
                      alignment=Qt.AlignmentFlag.AlignHCenter)

        btns = QHBoxLayout()
        self.proceed_btn = QPushButton("Proceed anyway")
        self.proceed_btn.clicked.connect(lambda: self._decide(PROCEED))
        self.skip_btn = QPushButton("Skip point")
        self.skip_btn.clicked.connect(lambda: self._decide(SKIP_POINT))
        self.abort_btn = QPushButton("Abort sweep")
        self.abort_btn.setObjectName("danger")
        self.abort_btn.clicked.connect(lambda: self._decide(ABORT_SWEEP))
        for b in (self.proceed_btn, self.skip_btn, self.abort_btn):
            b.setMinimumHeight(34)
            btns.addWidget(b)
        lay.addLayout(btns)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)
        self._timer.start(POLL_MS)
        self._tick()                        # populate immediately
        if self.sim:                        # parented → dies with the dialog
            self._sim_timer = QTimer(self)
            self._sim_timer.setSingleShot(True)
            self._sim_timer.timeout.connect(lambda: self._decide(PROCEED))
            self._sim_timer.start(SIM_AUTO_PROCEED_MS)

    # ── live update ──────────────────────────────────────────────────────
    def _fmt(self, value: Optional[float]) -> str:
        if value is None or (isinstance(value, float) and math.isnan(value)):
            return "—"
        return f"{value:,.0f}" if self.request.is_rpm else f"{value:.3f}"

    def _tick(self) -> None:
        try:
            mach, rpm = self.request.measure()
        except Exception:                              # noqa: BLE001
            mach, rpm = math.nan, math.nan
        req = self.request
        if req.is_rpm:
            value, target = rpm, req.target_rpm
            self.readback_lbl.setText(
                "measured Mach —" if math.isnan(mach)
                else f"measured Mach {mach:.3f}")
        else:
            value, target = mach, req.target_mach
            self.readback_lbl.setText(
                "fan — RPM" if math.isnan(rpm) else f"fan {rpm:,.0f} RPM")
        self.measured_lbl.setText(self._fmt(value))

        within = (not math.isnan(value)
                  and abs(value - target) <= req.tolerance)
        if math.isnan(value):
            self.delta_lbl.setText("Δ — NO MEASUREMENT")
            self.delta_lbl.setStyleSheet(self._delta_style(theme.ERROR))
        else:
            delta = value - target
            state = "IN TOLERANCE" if within else "OUT OF TOLERANCE"
            color = theme.SUCCESS if within else theme.WARNING
            if req.is_rpm:
                txt = (f"Δ {delta:+,.0f} RPM — {state} "
                       f"(±{req.tolerance:,.0f} RPM)")
            else:
                txt = f"Δ {delta:+.3f} — {state} (±{req.tolerance:g})"
            self.delta_lbl.setText(txt)
            self.delta_lbl.setStyleSheet(self._delta_style(color))

        now = time.monotonic()
        if within:
            if self._hold_t0 is None:
                self._hold_t0 = now
            held = now - self._hold_t0
            self.hold_lbl.setText(f"holding … {held:.1f}/"
                                  f"{self.settle_s:.1f} s → auto-proceed")
            if held >= self.settle_s:
                self._decide(PROCEED)
        else:
            self._hold_t0 = None
            self.hold_lbl.setText("waiting for the tunnel to reach the "
                                  "target…")

    @staticmethod
    def _delta_style(color: str) -> str:
        return ('font-family: "Consolas", monospace; font-size: 12pt; '
                f"font-weight: bold; color: {color};")

    # ── resolution ───────────────────────────────────────────────────────
    def _decide(self, decision: str) -> None:
        if self._decided:
            return
        self.decision = decision
        if decision == ABORT_SWEEP:
            self.reject()
        else:
            self.accept()

    def done(self, result: int) -> None:               # noqa: N802
        """Every path out (buttons, auto-proceed, Esc, X, E-STOP close)
        lands here exactly once — stop polling, freeze the decision."""
        self._decided = True
        self._timer.stop()
        super().done(result)
