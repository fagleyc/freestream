"""Calibration panel — two explicit routines per axis.

**1) Full calibration (slope + offset)** — two points:
   jog to a known position → enter its angle → *Capture point 1*;
   jog to a second known position → enter its angle → *Capture point 2*.
   Slope (clicks/degree) and offset both come from the pair.

**2) Offset re-zero (single point)** — once the slope is known:
   jog to a known position (e.g. a limit switch), enter its angle,
   *Set current position*. Only the offset moves; the slope is kept.

    angle = angle_high − (encoder_high − encoder) / clicks_per_degree
"""

from __future__ import annotations

from typing import Dict, Optional

from PyQt6.QtWidgets import (
    QDoubleSpinBox, QGroupBox, QHBoxLayout, QLabel, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)

from ac_delta import theme
from ac_delta.config import AxisConfig, CrescentConfig
from ac_delta.device import CrescentDrive


class _AxisCal(QGroupBox):
    def __init__(self, cfg: AxisConfig, device: CrescentDrive, parent=None):
        super().__init__(f"{cfg.name} calibration", parent)
        self.cfg = cfg
        self._device = device
        self._pt1: Optional[tuple] = None      # (angle, encoder)

        root = QVBoxLayout(self)
        root.setSpacing(4)
        root.setContentsMargins(8, 4, 8, 6)

        head = QHBoxLayout()
        head.addWidget(QLabel("Live encoder"))
        self.live_enc = QLabel("--")
        self.live_enc.setProperty("mono", "true")
        self.live_enc.setStyleSheet(
            f"color: {theme.ACCENT_LIGHT}; font-size: 13pt;")
        head.addWidget(self.live_enc)
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
        self.full_angle = self._angle_spin()
        r1.addWidget(self.full_angle)
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
        self.full_status = QLabel("jog to a known angle → capture pt 1 → "
                                  "jog to a second angle → capture pt 2")
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
        self.off_angle = self._angle_spin()
        r2.addWidget(self.off_angle)
        self.zero_btn = QPushButton("Set position = angle")
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
        r3.addWidget(QLabel("clicks/deg"))
        self.cpd = QDoubleSpinBox()
        self.cpd.setRange(-100000, 100000)
        self.cpd.setDecimals(4)
        self.cpd.setValue(cfg.clicks_per_degree)
        self.cpd.setFixedWidth(110)
        r3.addWidget(self.cpd)
        r3.addWidget(QLabel("angle_high"))
        self.angle_high = QDoubleSpinBox()
        self.angle_high.setRange(-360, 360)
        self.angle_high.setDecimals(4)
        self.angle_high.setValue(cfg.angle_high)
        self.angle_high.setFixedWidth(100)
        r3.addWidget(self.angle_high)
        r3.addWidget(QLabel("encoder_high"))
        self.encoder_high = QSpinBox()
        self.encoder_high.setRange(-32768, 32767)
        self.encoder_high.setValue(cfg.encoder_high)
        self.encoder_high.setFixedWidth(90)
        r3.addWidget(self.encoder_high)
        apply_btn = QPushButton("Apply")
        apply_btn.clicked.connect(self._apply_constants)
        r3.addWidget(apply_btn)
        r3.addStretch(1)
        root.addLayout(r3)

        self._update_state()

    @staticmethod
    def _angle_spin() -> QDoubleSpinBox:
        sp = QDoubleSpinBox()
        sp.setRange(-360, 360)
        sp.setDecimals(3)
        sp.setSuffix("°")
        sp.setFixedWidth(100)
        return sp

    # ── helpers ──
    def _live_encoder(self) -> Optional[int]:
        if not self._device.connected:
            return None
        return self._device.state()[self.cfg.name]["encoder"]

    def _sync_const_fields(self):
        self.cpd.setValue(self.cfg.clicks_per_degree)
        self.angle_high.setValue(self.cfg.angle_high)
        self.encoder_high.setValue(self.cfg.encoder_high)

    # ── routine 1 ──
    def _capture1(self):
        enc = self._live_encoder()
        if enc is None:
            self.full_status.setText("connect first")
            return
        self._pt1 = (float(self.full_angle.value()), enc)
        self.cap2_btn.setEnabled(True)
        self.full_status.setText(
            f"point 1: {self._pt1[0]:+.3f}° @ enc {self._pt1[1]:+d}   —   "
            f"now JOG to a second known angle, enter it, capture point 2")

    def _capture2(self):
        enc = self._live_encoder()
        if enc is None or self._pt1 is None:
            self.full_status.setText("capture point 1 first")
            return
        a2 = float(self.full_angle.value())
        try:
            cpd = self.cfg.calibrate_two_point(self._pt1[0], self._pt1[1],
                                               a2, enc)
        except ValueError as exc:
            self.full_status.setText(str(exc))
            return
        self.full_status.setText(
            f"point 2: {a2:+.3f}° @ enc {enc:+d}   →   slope "
            f"{cpd:.4f} clicks/deg — CALIBRATED")
        self._pt1 = None
        self.cap2_btn.setEnabled(False)
        self._sync_const_fields()
        self._update_state()

    # ── routine 2 ──
    def _offset_only(self):
        enc = self._live_encoder()
        if enc is None:
            self.off_status.setText("connect first")
            return
        try:
            self.cfg.calibrate_offset(float(self.off_angle.value()), enc)
        except ValueError as exc:
            self.off_status.setText(str(exc))
            return
        self.off_status.setText(
            f"offset set: {self.off_angle.value():+.3f}° @ enc {enc:+d} "
            f"(slope kept at {self.cfg.clicks_per_degree:.4f})")
        self._sync_const_fields()
        self._update_state()

    # ── constants ──
    def _apply_constants(self):
        cpd = float(self.cpd.value())
        if abs(cpd) < 1e-9:
            self.cal_state.setText("clicks/degree must be non-zero")
            return
        self.cfg.clicks_per_degree = cpd
        self.cfg.angle_high = float(self.angle_high.value())
        self.cfg.encoder_high = int(self.encoder_high.value())
        self.cfg.calibrated = True
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

    def refresh(self, encoder: int):
        self.live_enc.setText(f"{encoder:+d}")


class CalibrationPanel(QWidget):
    def __init__(self, cfg: CrescentConfig, device: CrescentDrive,
                 parent=None):
        super().__init__(parent)
        self._device = device
        # ONE layout for the widget's lifetime; set_config only swaps the
        # child widgets (installing a second QLayout on a widget is an
        # error and blanks the page).
        self._root = QVBoxLayout(self)
        self._root.setSpacing(6)
        self._populate(cfg)

    def _populate(self, cfg: CrescentConfig):
        self.alpha_cal = _AxisCal(cfg.alpha, self._device)
        self.beta_cal = _AxisCal(cfg.beta, self._device)
        self._root.addWidget(self.alpha_cal)
        self._root.addWidget(self.beta_cal)
        self._root.addStretch(1)

    def set_config(self, cfg: CrescentConfig):
        while self._root.count():
            item = self._root.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._populate(cfg)

    def refresh(self, state: Dict[str, dict]):
        self.alpha_cal.refresh(state["Alpha"]["encoder"])
        self.beta_cal.refresh(state["Beta"]["encoder"])
