"""Calibration panel — two explicit routines per axis, in inches.

**1) Full calibration (slope + offset)** — two points:
   move the stage (console) to a known position → enter its inches →
   *Capture point 1*; move to a second known position → enter its
   inches → *Capture point 2*. Slope (clicks/inch, signed) and offset
   both come from the pair.

**2) Offset re-zero (single point)** — once the slope is known:
   move the stage (console) to a known position (e.g. a scale mark),
   enter its inches, *Set current position*. Only the offset moves.

    inches = inch_high − (counts_high − counts) / clicks_per_inch

The 750-673 position counter zeroes at PLC power-up, so routine 2 is
needed once per rig power cycle; the slope persists. *Import legacy
XML…* pulls the signed slopes out of a C# sswtTraverseCalibrationFile.xml
(offsets in that file are stale by definition — re-zero after import).
"""

from __future__ import annotations

from typing import Dict, Optional

from PyQt6.QtWidgets import (
    QDoubleSpinBox, QFileDialog, QGroupBox, QHBoxLayout, QLabel,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from traverse_swt import theme
from traverse_swt.config import (AxisConfig, TraverseConfig,
                                 slopes_from_legacy_xml)
from traverse_swt.device import TraverseDrive


class _AxisCal(QGroupBox):
    def __init__(self, cfg: AxisConfig, device: TraverseDrive, parent=None):
        super().__init__(f"{cfg.name} ({cfg.label}) calibration", parent)
        self.cfg = cfg
        self._device = device
        self._pt1: Optional[tuple] = None      # (inches, counts)

        root = QVBoxLayout(self)
        root.setSpacing(4)
        root.setContentsMargins(8, 4, 8, 6)

        head = QHBoxLayout()
        head.addWidget(QLabel("Live counts"))
        self.live_cnt = QLabel("--")
        self.live_cnt.setProperty("mono", "true")
        self.live_cnt.setStyleSheet(
            f"color: {theme.ACCENT_LIGHT}; font-size: 13pt;")
        head.addWidget(self.live_cnt)
        head.addStretch(1)
        self.cal_state = QLabel("")
        head.addWidget(self.cal_state)
        root.addLayout(head)

        # ── routine 1: full two-point ──
        r1 = QHBoxLayout()
        r1.setSpacing(6)
        lbl1 = QLabel("1) Full cal:")
        lbl1.setStyleSheet(f"color: {theme.ACCENT_LIGHT}; "
                           f"font-weight: bold;")
        r1.addWidget(lbl1)
        r1.addWidget(QLabel("position ="))
        self.full_in = self._inch_spin()
        r1.addWidget(self.full_in)
        self.cap1_btn = QPushButton("Capture pt 1")
        self.cap1_btn.clicked.connect(self._capture1)
        r1.addWidget(self.cap1_btn)
        self.cap2_btn = QPushButton("Capture pt 2 → compute")
        self.cap2_btn.setObjectName("primary")
        self.cap2_btn.setEnabled(False)
        self.cap2_btn.clicked.connect(self._capture2)
        r1.addWidget(self.cap2_btn)
        r1.addStretch(1)
        root.addLayout(r1)
        self.full_status = QLabel("move the stage to a known position → "
                                  "capture pt 1 → move to a second "
                                  "position → capture pt 2")
        self.full_status.setObjectName("dim")
        root.addWidget(self.full_status)

        # ── routine 2: offset-only ──
        r2 = QHBoxLayout()
        r2.setSpacing(6)
        lbl2 = QLabel("2) Re-zero:")
        lbl2.setStyleSheet(f"color: {theme.ACCENT_LIGHT}; "
                           f"font-weight: bold;")
        r2.addWidget(lbl2)
        r2.addWidget(QLabel("position ="))
        self.off_in = self._inch_spin()
        r2.addWidget(self.off_in)
        self.zero_btn = QPushButton("Set current position")
        self.zero_btn.clicked.connect(self._offset_only)
        r2.addWidget(self.zero_btn)
        self.off_status = QLabel("(slope kept)")
        self.off_status.setObjectName("dim")
        r2.addWidget(self.off_status)
        r2.addStretch(1)
        root.addLayout(r2)

        # ── constants (view / direct entry), single compact row ──
        r3 = QHBoxLayout()
        r3.setSpacing(6)
        lbl3 = QLabel("Constants:")
        lbl3.setStyleSheet(f"color: {theme.ACCENT_LIGHT}; "
                           f"font-weight: bold;")
        r3.addWidget(lbl3)
        r3.addWidget(QLabel("clicks/in"))
        self.cpi = QDoubleSpinBox()
        self.cpi.setRange(-2_000_000, 2_000_000)
        self.cpi.setDecimals(1)
        self.cpi.setValue(cfg.clicks_per_inch)
        self.cpi.setFixedWidth(120)
        r3.addWidget(self.cpi)
        r3.addWidget(QLabel("inch_high"))
        self.inch_high = QDoubleSpinBox()
        self.inch_high.setRange(-100, 100)
        self.inch_high.setDecimals(4)
        self.inch_high.setValue(cfg.inch_high)
        self.inch_high.setFixedWidth(100)
        r3.addWidget(self.inch_high)
        r3.addWidget(QLabel("counts_high"))
        self.counts_high = QSpinBox()
        # absolute (unwrapped) counts are unbounded — allow the full
        # int32 range the widget supports
        self.counts_high.setRange(-2_000_000_000, 2_000_000_000)
        self.counts_high.setValue(cfg.counts_high)
        self.counts_high.setFixedWidth(110)
        r3.addWidget(self.counts_high)
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._apply_constants)
        r3.addWidget(apply_btn)
        r3.addStretch(1)
        root.addLayout(r3)

        self._update_state()

    @staticmethod
    def _inch_spin() -> QDoubleSpinBox:
        sp = QDoubleSpinBox()
        sp.setRange(-100, 100)
        sp.setDecimals(3)
        sp.setSuffix('"')
        sp.setFixedWidth(100)
        return sp

    # ── helpers ──
    def _live_counts(self) -> Optional[int]:
        if not self._device.connected:
            return None
        return self._device.state()[self.cfg.name]["counts"]

    def _sync_const_fields(self):
        self.cpi.setValue(self.cfg.clicks_per_inch)
        self.inch_high.setValue(self.cfg.inch_high)
        self.counts_high.setValue(self.cfg.counts_high)

    # ── routine 1 ──
    def _capture1(self):
        cnt = self._live_counts()
        if cnt is None:
            self.full_status.setText("connect first")
            return
        self._pt1 = (float(self.full_in.value()), cnt)
        self.cap2_btn.setEnabled(True)
        self.full_status.setText(
            f"point 1: {self._pt1[0]:+.3f}\" @ cnt {self._pt1[1]:+d}   —   "
            f"now MOVE to a second known position, enter it, capture pt 2")

    def _capture2(self):
        cnt = self._live_counts()
        if cnt is None or self._pt1 is None:
            self.full_status.setText("capture point 1 first")
            return
        p2 = float(self.full_in.value())
        try:
            cpi = self.cfg.calibrate_two_point(self._pt1[0], self._pt1[1],
                                               p2, cnt)
        except ValueError as exc:
            self.full_status.setText(str(exc))
            return
        self.full_status.setText(
            f"point 2: {p2:+.3f}\" @ cnt {cnt:+d}   →   slope "
            f"{cpi:.1f} clicks/in — CALIBRATED")
        self._pt1 = None
        self.cap2_btn.setEnabled(False)
        self._sync_const_fields()
        self._update_state()

    # ── routine 2 ──
    def _offset_only(self):
        cnt = self._live_counts()
        if cnt is None:
            self.off_status.setText("connect first")
            return
        try:
            self.cfg.calibrate_offset(float(self.off_in.value()), cnt)
        except ValueError as exc:
            self.off_status.setText(str(exc))
            return
        self.off_status.setText(
            f"offset set: {self.off_in.value():+.3f}\" @ cnt {cnt:+d} "
            f"(slope kept at {self.cfg.clicks_per_inch:.1f})")
        self._sync_const_fields()
        self._update_state()

    # ── constants ──
    def _apply_constants(self):
        cpi = float(self.cpi.value())
        if abs(cpi) < 1e-9:
            self.cal_state.setText("clicks/inch must be non-zero")
            return
        self.cfg.clicks_per_inch = cpi
        self.cfg.inch_high = float(self.inch_high.value())
        self.cfg.counts_high = int(self.counts_high.value())
        self.cfg.calibrated = True
        self._update_state()

    def set_slope(self, cpi: float):
        """Slope from an external source (legacy XML import)."""
        self.cfg.clicks_per_inch = cpi
        self.cfg.calibrated = False        # offset must be re-zeroed
        self._sync_const_fields()
        self._update_state()

    def _update_state(self):
        if self.cfg.calibrated:
            self.cal_state.setText("CALIBRATED")
            self.cal_state.setStyleSheet(f"color: {theme.SUCCESS}; "
                                         f"font-weight: bold;")
        else:
            self.cal_state.setText("UNCALIBRATED — jog only")
            self.cal_state.setStyleSheet(f"color: {theme.WARNING}; "
                                         f"font-weight: bold;")

    def refresh(self, counts: int):
        self.live_cnt.setText(f"{counts:+d}")


class CalibrationPanel(QWidget):
    def __init__(self, cfg: TraverseConfig, device: TraverseDrive,
                 parent=None):
        super().__init__(parent)
        self._device = device
        # ONE layout for the widget's lifetime; set_config only swaps the
        # child widgets (installing a second QLayout on a widget is an
        # error and blanks the page).
        self._root = QVBoxLayout(self)
        self._root.setSpacing(6)
        self._populate(cfg)

    def _populate(self, cfg: TraverseConfig):
        top = QHBoxLayout()
        imp_btn = QPushButton("Import legacy C# cal XML…")
        imp_btn.setToolTip("Signed slopes from a "
                           "sswtTraverseCalibrationFile.xml — offsets are "
                           "stale, re-zero afterwards")
        imp_btn.clicked.connect(self._import_xml)
        top.addWidget(imp_btn)
        self.import_status = QLabel("")
        self.import_status.setObjectName("dim")
        top.addWidget(self.import_status)
        top.addStretch(1)
        holder = QWidget()
        holder.setLayout(top)
        self._root.addWidget(holder)

        self.axis_cals: Dict[str, _AxisCal] = {}
        for ax_cfg in cfg.axes():
            w = _AxisCal(ax_cfg, self._device)
            self.axis_cals[ax_cfg.name] = w
            self._root.addWidget(w)
        self._root.addStretch(1)

    def set_config(self, cfg: TraverseConfig):
        while self._root.count():
            item = self._root.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._populate(cfg)

    def _import_xml(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Legacy calibration XML", "", "XML (*.xml)")
        if not path:
            return
        try:
            slopes = slopes_from_legacy_xml(path)
        except (ValueError, OSError) as exc:
            self.import_status.setText(f"import failed: {exc}")
            return
        for name, cpi in slopes.items():
            self.axis_cals[name].set_slope(cpi)
        self.import_status.setText(
            "slopes imported (" +
            ", ".join(f"{n} {v:.1f}" for n, v in slopes.items()) +
            ") — RE-ZERO each axis before trusting inches")

    def refresh(self, state: Dict[str, dict]):
        for name, w in self.axis_cals.items():
            w.refresh(state[name]["counts"])
