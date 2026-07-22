"""Settings dialog — loop tuning, per-axis limits, host-side homing."""

from __future__ import annotations

import logging

from PyQt6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QSpinBox, QTabWidget, QVBoxLayout,
    QWidget,
)

from traverse_swt.config import AxisConfig, TraverseConfig

log = logging.getLogger(__name__)


class _AxisRows:
    """Widget bundle for one axis's group box."""

    def __init__(self, cfg: AxisConfig, box: QGroupBox):
        f = QFormLayout(box)
        self.min_in, self.max_in = _pair(cfg.min_in, cfg.max_in)
        f.addRow("Soft limits min / max (in)", _row(self.min_in,
                                                    self.max_in))
        self.tol = QDoubleSpinBox()
        self.tol.setRange(0.001, 1.0)
        self.tol.setDecimals(3)
        self.tol.setValue(cfg.tolerance_in)
        self.tol.setSuffix('"')
        f.addRow("Move tolerance", self.tol)

        self.enabled = QCheckBox("Axis enabled")
        self.enabled.setChecked(cfg.enabled)
        f.addRow("", self.enabled)

        self.fwd_up = QCheckBox("'Forward' bit increases counts")
        self.fwd_up.setChecked(cfg.fwd_increases_counts)
        self.fwd_up.setToolTip(
            "Flip if a supervised move shows counts moving opposite to "
            "the commanded direction (move_to also trips WRONG WAY)")
        f.addRow("", self.fwd_up)

        self.limit_en = QCheckBox("Limit switch input enabled")
        self.limit_en.setChecked(cfg.limit_enabled)
        self.limit_en.setToolTip(
            "Honor this axis's StatusWord limit bit (homing + runtime "
            "reaction). X ships disabled per the rig — its limit input "
            "is ignored entirely.")
        f.addRow("", self.limit_en)

    def apply(self, cfg: AxisConfig) -> None:
        cfg.min_in = min(self.min_in.value(), self.max_in.value())
        cfg.max_in = max(self.min_in.value(), self.max_in.value())
        cfg.tolerance_in = self.tol.value()
        cfg.enabled = self.enabled.isChecked()
        cfg.fwd_increases_counts = self.fwd_up.isChecked()
        cfg.limit_enabled = self.limit_en.isChecked()


def _pair(lo, hi):
    a = QDoubleSpinBox()
    a.setRange(-100, 100)
    a.setDecimals(3)
    a.setValue(lo)
    a.setSuffix('"')
    b = QDoubleSpinBox()
    b.setRange(-100, 100)
    b.setDecimals(3)
    b.setValue(hi)
    b.setSuffix('"')
    return a, b


def _row(*widgets):
    row = QHBoxLayout()
    for w in widgets:
        row.addWidget(w)
    return row


class SettingsDialog(QDialog):
    def __init__(self, cfg: TraverseConfig, parent=None, drive=None):
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self._cfg = cfg
        self._drive = drive        # kept for API compat with hosts
        self._build(cfg)

    def _build(self, cfg: TraverseConfig):
        # Two tabs so the dialog fits on screen: General (loop + axis
        # basics) and Advanced (host-side homing tuning).
        root = QVBoxLayout(self)
        tabs = QTabWidget()
        general = QWidget()
        gen = QVBoxLayout(general)
        advanced = QWidget()
        adv = QVBoxLayout(advanced)
        tabs.addTab(general, "General")
        tabs.addTab(advanced, "Advanced")
        root.addWidget(tabs)

        loop = QGroupBox("Control loop / Modbus")
        lf = QFormLayout(loop)
        self.loop_ms = QSpinBox()
        self.loop_ms.setRange(20, 1000)
        self.loop_ms.setValue(cfg.loop_ms)
        self.loop_ms.setSuffix(" ms")
        self.loop_ms.setToolTip("Poll/command period. The PLC runs at a "
                                "fixed 2000 steps/s — a faster loop stops "
                                "moves more precisely. (While homing the "
                                "loop tightens on its own for the limit-"
                                "bit poll.)")
        lf.addRow("Loop period", self.loop_ms)
        self.timeout = QDoubleSpinBox()
        self.timeout.setRange(0.1, 10.0)
        self.timeout.setDecimals(1)
        self.timeout.setValue(cfg.modbus_timeout_s)
        self.timeout.setSuffix(" s")
        lf.addRow("Modbus timeout", self.timeout)
        self.max_err = QSpinBox()
        self.max_err.setRange(1, 50)
        self.max_err.setValue(cfg.max_consecutive_errors)
        self.max_err.setToolTip("Watchdog: consecutive comm failures "
                                "before stopping all axes")
        lf.addRow("Watchdog error count", self.max_err)
        self.dwell_ms = QSpinBox()
        self.dwell_ms.setRange(0, 5000)
        self.dwell_ms.setValue(cfg.direction_dwell_ms)
        self.dwell_ms.setSuffix(" ms")
        self.dwell_ms.setToolTip(
            "Commanded-stop dwell before any start or direction "
            "reversal — keep it longer than the PLC's 250 ms stop "
            "sequence so the stepper modules never get conflicting "
            "commands (the start/stop fault protection). Speed and "
            "accel themselves are FIXED in the PLC program.")
        lf.addRow("Direction-change dwell", self.dwell_ms)
        gen.addWidget(loop)

        self._axis_rows = {}
        for ax_cfg in cfg.axes():
            box = QGroupBox(f"{ax_cfg.name} — {ax_cfg.label}")
            self._axis_rows[ax_cfg.name] = _AxisRows(ax_cfg, box)
            gen.addWidget(box)

        # ── host-side homing (StatusWord limit bits + jog) ──
        homing = QGroupBox("Homing (host)")
        hf = QFormLayout(homing)
        self._home_rows = {}
        for ax_cfg in cfg.axes():
            enabled = QCheckBox("homing enabled")
            enabled.setChecked(ax_cfg.home_enabled)
            enabled.setToolTip(
                "Home this axis to its limit switch (StatusWord bit, "
                "host-side jog sequence). X ships disabled — no homing "
                "on the axial axis.")
            datum = QDoubleSpinBox()
            datum.setRange(-100.0, 100.0)
            datum.setDecimals(3)
            datum.setValue(ax_cfg.home_datum_in)
            datum.setSuffix('"')
            datum.setToolTip(
                "What the limit position reads after homing "
                "(calibrate_offset is called there). Default −18.0\".")
            seek_fwd = QCheckBox("seek jogs FWD bit")
            seek_fwd.setChecked(ax_cfg.home_jog_fwd)
            seek_fwd.setToolTip(
                "BIT-LEVEL homing direction, set empirically on the rig "
                "— deliberately independent of the position-mode "
                "direction sense (they need OPPOSITE senses on this "
                "rig). Ticked: the homing seek jogs this axis's FWD "
                "ControlWord bit (backoff jogs REV); unticked: the "
                "opposite. Runtime limit recovery is always the "
                "opposite bit.")
            self._home_rows[ax_cfg.name] = (enabled, datum, seek_fwd)
            hf.addRow(f"{ax_cfg.name} homing / datum / seek",
                      _row(enabled, datum, seek_fwd))
        self.backoff_margin = QDoubleSpinBox()
        self.backoff_margin.setRange(0.0, 5.0)
        self.backoff_margin.setDecimals(2)
        self.backoff_margin.setSingleStep(0.05)
        self.backoff_margin.setValue(cfg.home_backoff_margin_s)
        self.backoff_margin.setSuffix(" s")
        self.backoff_margin.setToolTip(
            "After the limit bit clears during backoff, keep jogging "
            "away this long before stopping. The PLC speed is fixed "
            "(no host 'slow' jog) — this margin time bounds the "
            "overshoot past the switch release point.")
        hf.addRow("Backoff margin", self.backoff_margin)
        self.seek_timeout = QDoubleSpinBox()
        self.seek_timeout.setRange(1.0, 3600.0)
        self.seek_timeout.setDecimals(0)
        self.seek_timeout.setValue(cfg.home_seek_timeout_s)
        self.seek_timeout.setSuffix(" s")
        self.seek_timeout.setToolTip(
            "Deadline for the seek toward the limit; on breach the "
            "axis is stopped and the cycle faults (not homed)")
        self.backoff_timeout = QDoubleSpinBox()
        self.backoff_timeout.setRange(1.0, 600.0)
        self.backoff_timeout.setDecimals(0)
        self.backoff_timeout.setValue(cfg.home_backoff_timeout_s)
        self.backoff_timeout.setSuffix(" s")
        self.backoff_timeout.setToolTip(
            "Deadline for backing off the switch; on breach the axis "
            "is stopped and the cycle faults (not homed)")
        hf.addRow("Seek / backoff timeout",
                  _row(self.seek_timeout, self.backoff_timeout))
        self.limit_low = QCheckBox(
            "Limit bits active-low (bit clears when a switch is engaged)")
        self.limit_low.setChecked(cfg.limit_active_low)
        self.limit_low.setToolTip(
            "Rig-verified 2026-07-22: the NC chain drives each limit "
            "bit HIGH when healthy; it CLEARS when the switch is "
            "pressed. Untick only if the PLC-side wiring changes back.")
        hf.addRow("", self.limit_low)
        note = QLabel(
            "Homing is per-power-cycle: a module power cycle zeroes the "
            "position counter — re-home each setup. The offset persists "
            "only if you save the config / Set as Defaults.")
        note.setObjectName("dim")
        note.setWordWrap(True)
        hf.addRow(note)
        adv.addWidget(homing)
        gen.addStretch(1)
        adv.addStretch(1)

        note = QLabel("Loop changes apply at the next tick; limits apply "
                      "to new move commands. Direction/polarity flips "
                      "apply immediately — flip them only while stopped.")
        note.setObjectName("dim")
        note.setWordWrap(True)
        root.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def accept(self) -> None:
        cfg = self._cfg
        cfg.loop_ms = self.loop_ms.value()
        cfg.modbus_timeout_s = self.timeout.value()
        cfg.max_consecutive_errors = self.max_err.value()
        cfg.direction_dwell_ms = self.dwell_ms.value()
        for ax_cfg in cfg.axes():
            self._axis_rows[ax_cfg.name].apply(ax_cfg)
        for ax_cfg in cfg.axes():
            enabled, datum, seek_fwd = self._home_rows[ax_cfg.name]
            ax_cfg.home_enabled = enabled.isChecked()
            ax_cfg.home_datum_in = datum.value()
            ax_cfg.home_jog_fwd = seek_fwd.isChecked()
        cfg.home_backoff_margin_s = self.backoff_margin.value()
        cfg.home_seek_timeout_s = self.seek_timeout.value()
        cfg.home_backoff_timeout_s = self.backoff_timeout.value()
        cfg.limit_active_low = self.limit_low.isChecked()
        super().accept()
