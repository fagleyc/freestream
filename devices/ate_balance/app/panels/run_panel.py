"""Run panel — full-rate time histories, point sampling, dwell averaging,
results table, export.

The time-history plots draw every frame in the ring buffer (300 Hz on the
rig), not a UI-rate subsample.  Dwell averaging is driven by the parent panel
(which owns the :class:`DwellAccumulator` fed by the live frame stream); this
panel emits ``startDwell``/``stopDwell`` and renders the resulting points.
"""

from __future__ import annotations

import csv
from typing import List

import numpy as np
from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFileDialog, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QPushButton, QSpinBox, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from ate_balance.datamodel import ReducedPoint, RingBuffer, TestCase
from ate_balance.device import AteBalanceDevice
from ate_balance.app.plots import TimeHistory

# Loads only — the balance measures forces and moments, nothing aerodynamic.
_COLUMNS = ["alpha", "beta", "n",
            "Lift (N)", "Drag (N)", "Side (N)",
            "Roll (N·m)", "Pitch (N·m)", "Yaw (N·m)"]
_MEAN_KEYS = ["Lift", "Drag", "Side", "Roll", "Pitch", "Yaw"]

_WINDOWS = [("5 s", 5.0), ("10 s", 10.0), ("30 s", 30.0), ("60 s", 60.0)]


class RunPanel(QWidget):
    startDwell = pyqtSignal(float, float)
    stopDwell = pyqtSignal()

    def __init__(self, device: AteBalanceDevice, ring: RingBuffer, parent=None):
        super().__init__(parent)
        self._dev = device
        self._points: List[ReducedPoint] = []
        self._build(ring)

    def _build(self, ring: RingBuffer):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        # ── controls row: sample | dwell | plot window ──
        controls = QHBoxLayout()

        samp = QGroupBox("Single sample (OGI average)")
        sl = QHBoxLayout(samp)
        sl.addWidget(QLabel("Duration"))
        self.sample_secs = QSpinBox()
        self.sample_secs.setRange(1, 300)
        self.sample_secs.setValue(self._dev.config.default_sample_seconds)
        self.sample_secs.setSuffix(" s")
        sl.addWidget(self.sample_secs)
        take = QPushButton("Take Sample")
        take.setObjectName("primary")
        take.clicked.connect(lambda: self._dev.take_sample(self.sample_secs.value()))
        sl.addWidget(take)
        controls.addWidget(samp)

        dwell = QGroupBox("Dwell point (live average)")
        dl = QHBoxLayout(dwell)
        dl.addWidget(QLabel("α"))
        self.alpha_spin = QDoubleSpinBox()
        self.alpha_spin.setRange(-90, 90)
        self.alpha_spin.setDecimals(2)
        self.alpha_spin.setSuffix("°")
        dl.addWidget(self.alpha_spin)
        dl.addWidget(QLabel("β"))
        self.beta_spin = QDoubleSpinBox()
        self.beta_spin.setRange(-90, 90)
        self.beta_spin.setDecimals(2)
        self.beta_spin.setSuffix("°")
        dl.addWidget(self.beta_spin)
        self.dwell_btn = QPushButton("Start Dwell")
        self.dwell_btn.setObjectName("success")
        self.dwell_btn.clicked.connect(self._toggle_dwell)
        dl.addWidget(self.dwell_btn)
        self.dwell_lbl = QLabel("idle")
        self.dwell_lbl.setProperty("mono", "true")
        dl.addWidget(self.dwell_lbl)
        controls.addWidget(dwell)

        disp = QGroupBox("Time history")
        pl = QHBoxLayout(disp)
        pl.addWidget(QLabel("Window"))
        self.window_combo = QComboBox()
        for label, _secs in _WINDOWS:
            self.window_combo.addItem(label)
        self.window_combo.setCurrentIndex(1)          # 10 s
        self.window_combo.currentIndexChanged.connect(self._window_changed)
        pl.addWidget(self.window_combo)
        self.pause_btn = QPushButton("Pause")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._toggle_pause)
        pl.addWidget(self.pause_btn)
        controls.addWidget(disp)

        controls.addStretch(1)
        root.addLayout(controls)

        # ── plots over table ──
        split = QSplitter(Qt.Orientation.Vertical)

        self.history = TimeHistory(ring)
        split.addWidget(self.history)

        bottom = QWidget()
        bl = QVBoxLayout(bottom)
        bl.setContentsMargins(0, 0, 0, 0)
        self.sample_lbl = QLabel("Last sample: —")
        self.sample_lbl.setProperty("mono", "true")
        self.sample_lbl.setObjectName("dim")
        bl.addWidget(self.sample_lbl)
        self.table = QTableWidget(0, len(_COLUMNS))
        self.table.setHorizontalHeaderLabels(_COLUMNS)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().setVisible(False)
        bl.addWidget(self.table, 1)

        ex = QHBoxLayout()
        ex.addStretch(1)
        clr = QPushButton("Clear")
        clr.clicked.connect(self._clear)
        ex.addWidget(clr)
        csv_btn = QPushButton("Export CSV…")
        csv_btn.clicked.connect(self._export_csv)
        ex.addWidget(csv_btn)
        npz_btn = QPushButton("Export TestCase (.npz)…")
        npz_btn.clicked.connect(self._export_npz)
        ex.addWidget(npz_btn)
        bl.addLayout(ex)

        split.addWidget(bottom)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        root.addWidget(split, 1)

        self._dwell_active = False

    # ── time-history controls ──
    def _window_changed(self, idx: int) -> None:
        self.history.window_s = _WINDOWS[idx][1]

    def _toggle_pause(self, paused: bool) -> None:
        self.history.paused = paused
        self.pause_btn.setText("Resume" if paused else "Pause")

    def refresh_plots(self) -> None:
        """Called at UI rate by the parent panel."""
        self.history.refresh()

    # ── dwell control ──
    def _toggle_dwell(self):
        if not self._dwell_active:
            self.startDwell.emit(self.alpha_spin.value(), self.beta_spin.value())
        else:
            self.stopDwell.emit()

    def set_dwell_state(self, active: bool, n: int = 0):
        self._dwell_active = active
        if active:
            self.dwell_btn.setText("Stop Dwell")
            self.dwell_lbl.setText(f"averaging… n={n}")
        else:
            self.dwell_btn.setText("Start Dwell")
            self.dwell_lbl.setText("idle")

    # ── updates from replies / dwell ──
    def show_sample(self, kind: str, named: dict):
        txt = "  ".join(f"{k}={named.get(k, 0.0):.2f}"
                        for k in ("Lift", "Drag", "Side", "Pitch", "Yaw", "Roll"))
        self.sample_lbl.setText(f"Last {kind}:  {txt}")

    def add_point(self, rp: ReducedPoint):
        self._points.append(rp)
        r = self.table.rowCount()
        self.table.insertRow(r)
        vals = [rp.alpha, rp.beta, rp.n_samples] + \
               [rp.means.get(k, 0.0) for k in _MEAN_KEYS]
        stds = [None, None, None] + [rp.stds.get(k, 0.0) for k in _MEAN_KEYS]
        for c, val in enumerate(vals):
            txt = f"{int(val)}" if c == 2 else f"{val:.3f}"
            item = QTableWidgetItem(txt)
            item.setTextAlignment(Qt.AlignmentFlag.AlignRight |
                                  Qt.AlignmentFlag.AlignVCenter)
            if stds[c] is not None:
                item.setToolTip(f"±{stds[c]:.3f} (1σ over {rp.n_samples} frames)")
            self.table.setItem(r, c, item)
        self.table.scrollToBottom()

    def _clear(self):
        self._points = []
        self.table.setRowCount(0)

    # ── export ──
    def _export_csv(self):
        if not self._points:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export reduced points",
                                              "ate_run.csv", "CSV (*.csv)")
        if not path:
            return
        rows = [p.as_row() for p in self._points]
        keys = list(rows[0].keys())
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            w.writerows(rows)

    def _export_npz(self):
        """Save a Streamlined-shaped TestCase as a .npz of named arrays."""
        if not self._points:
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export TestCase",
                                              "ate_run.npz", "NumPy (*.npz)")
        if not path:
            return
        tc = TestCase.from_reduced_points(self._points, name="ATE run")
        np.savez(path,
                 alphas=tc.alphas, betas=tc.betas,
                 lift_forces=tc.lift_forces, drag_forces=tc.drag_forces,
                 side_forces=tc.side_forces, roll_moments=tc.roll_moments,
                 pitch_moments=tc.pitch_moments, yaw_moments=tc.yaw_moments,
                 Q=tc.tunnel_conditions.Q)
