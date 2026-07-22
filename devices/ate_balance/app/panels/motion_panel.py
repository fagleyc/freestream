"""Motion & balance panel — yaw/incidence drives, locking, tare, E-stop.

Owns the ONE editor for the model-span configuration (``AteConfig.
span_config``): a combo that relabels the two drive boxes so the operator
is never misled about which physical drive a logical axis commands —
full span: alpha = incidence, beta = yaw; ½ span: alpha = the YAW drive
and the incidence controls are disabled (that drive is unused).
"""

from __future__ import annotations

from PyQt6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QPushButton, QVBoxLayout, QWidget,
)

from ate_balance.config import SPAN_FULL, SPAN_HALF
from ate_balance.device import AteBalanceDevice
from ate_balance.protocol import INC_LIMITS_DEG, YAW_LIMITS_DEG

#: drive-box titles per span_config: (yaw title, incidence title)
_SPAN_TITLES = {
    SPAN_FULL: ("Yaw drive — β", "Incidence drive — α"),
    SPAN_HALF: ("Yaw drive — α (½-span)",
                "Incidence drive — unused (½-span)"),
}
_INC_UNUSED_TIP = ("Unused in the ½-span configuration — the semispan "
                   "model's angle of attack (α) is the YAW drive.")


class MotionPanel(QWidget):
    def __init__(self, device: AteBalanceDevice, parent=None):
        super().__init__(parent)
        self._dev = device
        self._build()

    def _build(self):
        root = QVBoxLayout(self)

        # ── model span configuration (single editor for span_config) ──
        span_row = QHBoxLayout()
        span_row.addWidget(QLabel("Model span"))
        self.span_combo = QComboBox()
        self.span_combo.addItem("Full span (α = incidence, β = yaw)",
                                SPAN_FULL)
        self.span_combo.addItem("½ span (α = yaw drive, no β)", SPAN_HALF)
        self.span_combo.setToolTip(
            "Full span: alpha commands the incidence drive, beta the yaw "
            "drive.\n½ span: the semispan model on the turntable gets its "
            "angle of attack\nfrom the YAW drive — alpha commands yaw, "
            "there is no beta axis and the\nincidence drive is unused. "
            "Recorded into the data files as span_config.")
        self.span_combo.currentIndexChanged.connect(self._span_changed)
        span_row.addWidget(self.span_combo)
        self.span_note = QLabel("")
        self.span_note.setObjectName("dim")
        span_row.addWidget(self.span_note)
        span_row.addStretch(1)
        root.addLayout(span_row)

        row = QHBoxLayout()
        self._yaw_box = self._axis_box(
            "Yaw drive", YAW_LIMITS_DEG, self._dev.goto_yaw,
            lambda: self._dev.goto_yaw(0.0), "yaw")
        row.addWidget(self._yaw_box)
        self._inc_box = self._axis_box(
            "Incidence drive", INC_LIMITS_DEG, self._dev.goto_inc,
            lambda: self._dev.goto_inc(0.0), "inc")
        row.addWidget(self._inc_box)
        row.addWidget(self._balance_box())
        row.addStretch(1)
        root.addLayout(row, 1)

        self.refresh_span()

    # ── span configuration ──
    def _span_changed(self, index: int) -> None:
        self._dev.config.span_config = self.span_combo.itemData(index)
        self._apply_span()

    def refresh_span(self) -> None:
        """Re-mirror the combo + drive boxes from the live config (used
        after a config load/apply edits span_config behind the combo)."""
        span = self._dev.span_config
        idx = self.span_combo.findData(span)
        if idx >= 0 and idx != self.span_combo.currentIndex():
            self.span_combo.blockSignals(True)
            self.span_combo.setCurrentIndex(idx)
            self.span_combo.blockSignals(False)
        self._apply_span()

    def _apply_span(self) -> None:
        """Relabel/disable the drive boxes so the mapping is unmistakable."""
        half = self._dev.half_span
        yaw_title, inc_title = _SPAN_TITLES[SPAN_HALF if half else SPAN_FULL]
        self._yaw_box.setTitle(yaw_title)
        self._inc_box.setTitle(inc_title)
        self._inc_box.setEnabled(not half)
        self._inc_box.setToolTip(_INC_UNUSED_TIP if half else "")
        self.span_note.setText(
            "α jogs the YAW drive; incidence is not commanded" if half
            else "")

    def _axis_box(self, title, limits, go_fn, zero_fn, key) -> QGroupBox:
        box = QGroupBox(title)
        g = QGridLayout(box)

        g.addWidget(QLabel("Position"), 0, 0)
        pos = QLabel("--")
        pos.setObjectName("value")
        setattr(self, f"_{key}_pos", pos)
        g.addWidget(pos, 0, 1)
        g.addWidget(QLabel("deg"), 0, 2)

        g.addWidget(QLabel("Target"), 1, 0)
        spin = QDoubleSpinBox()
        spin.setRange(limits[0], limits[1])
        spin.setDecimals(2)
        spin.setSingleStep(0.5)
        setattr(self, f"_{key}_spin", spin)
        g.addWidget(spin, 1, 1)
        g.addWidget(QLabel(f"[{limits[0]:.0f}, {limits[1]:.0f}]"), 1, 2)

        move = QPushButton("Move")
        move.setObjectName("primary")
        move.clicked.connect(lambda: go_fn(spin.value()))
        g.addWidget(move, 2, 0)

        stop = QPushButton("Stop")
        stop.clicked.connect(self._dev.stop_all_motion)
        g.addWidget(stop, 2, 1)

        tozero = QPushButton("To Zero")
        tozero.clicked.connect(lambda: zero_fn())
        g.addWidget(tozero, 2, 2)
        return box

    def _balance_box(self) -> QGroupBox:
        box = QGroupBox("Balance")
        g = QGridLayout(box)

        g.addWidget(QLabel("Lock status"), 0, 0)
        self._lock_lbl = QLabel("--")
        self._lock_lbl.setProperty("mono", "true")
        g.addWidget(self._lock_lbl, 0, 1, 1, 2)

        lock = QPushButton("Lock")
        lock.clicked.connect(self._dev.lock)
        g.addWidget(lock, 1, 0)
        unlock = QPushButton("Unlock")
        unlock.clicked.connect(self._dev.unlock)
        g.addWidget(unlock, 1, 1)
        status = QPushButton("Status")
        status.clicked.connect(self._dev.get_lock_status)
        g.addWidget(status, 1, 2)

        zero = QPushButton("Zero (tare)")
        zero.setObjectName("success")
        zero.clicked.connect(self._dev.zero)
        g.addWidget(zero, 2, 0, 1, 2)

        estop = QPushButton("E-STOP")
        estop.setObjectName("danger")
        estop.clicked.connect(self._dev.stop_all_motion)
        g.addWidget(estop, 2, 2)
        return box

    # ── reply-driven updates (called on GUI thread) ──
    def set_positions(self, yaw: float, inc: float) -> None:
        self._yaw_pos.setText(f"{yaw:6.2f}")
        self._inc_pos.setText(f"{inc:6.2f}")

    def set_lock_status(self, text: str) -> None:
        self._lock_lbl.setText(text)
