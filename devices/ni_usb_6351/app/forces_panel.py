"""Forces panel — live balance forces/moments from a .vol calibration.

Load a Streamlined ``.vol`` file → the cal matrix is computed (Linear /
Quadratic / Cubic) and every UI tick the ring buffer's bridge voltages are
pushed through the Streamlined pipeline (volts/excitation → elements →
body-frame Fx…Mz). Per-element loads are compared against the balance's
rated maxima with amber/red overstress warnings.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Optional

import numpy as np
import pyqtgraph as pg
from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox, QFileDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QProgressBar, QPushButton, QVBoxLayout, QWidget,
)

from ni_usb_6351 import balcal, theme
from ni_usb_6351.config import NiDaqConfig
from ni_usb_6351.datamodel import ScanRingBuffer

from .plots import _style_plot

_FORCES = [("Fx", "lb"), ("Fy", "lb"), ("Fz", "lb"),
           ("Mx", "in·lb"), ("My", "in·lb"), ("Mz", "in·lb")]


class _ForceTile(QFrame):
    def __init__(self, name: str, unit: str, color: str, parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            f"QFrame {{ background-color: {theme.BG_LIGHT}; "
            f"border: 1px solid {theme.BORDER}; border-radius: 6px; }}"
            f"QLabel {{ border: none; background: transparent; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 6, 10, 6)
        lay.setSpacing(1)
        head = QHBoxLayout()
        chip = QLabel()
        chip.setFixedSize(9, 9)
        chip.setStyleSheet(f"background-color: {color}; "
                           f"border-radius: 4px;")
        head.addWidget(chip)
        t = QLabel(name)
        t.setStyleSheet(f"color: {theme.TEXT_DIM}; font-weight: bold;")
        head.addWidget(t)
        head.addStretch(1)
        lay.addLayout(head)
        self.value = QLabel("--")
        self.value.setStyleSheet(
            "font-family: 'Segoe UI'; font-size: 16pt; font-weight: 600; "
            f"color: {theme.TEXT};")
        lay.addWidget(self.value)
        u = QLabel(unit)
        u.setObjectName("dim")
        lay.addWidget(u)

    def update_value(self, v: float):
        mag = abs(v)
        dec = 4 if mag < 1 else (3 if mag < 10 else 2)
        self.value.setText(f"{v:+,.{dec}f}")


class ForcesPanel(QWidget):
    """Calibration + live forces + load-limit monitoring."""

    #: emitted when the operator picks a balance layout (Force|Moment); the
    #: coordinator routes it to the driver's ``set_balance_config`` so the
    #: four bridge channels rename on the live device (single source of
    #: truth = the driver config, never this combo alone).
    balanceConfigChanged = pyqtSignal(str)

    def __init__(self, cfg: NiDaqConfig, parent=None):
        super().__init__(parent)
        self.config = cfg
        self.cal: Optional[balcal.BalanceCalibration] = None
        self.overstress: bool = False
        self._build()

    # ── UI ──
    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(6)

        # calibration bar
        cal_bar = QHBoxLayout()
        self.load_btn = QPushButton("Load .vol…")
        self.load_btn.setObjectName("primary")
        self.load_btn.clicked.connect(self._browse_vol)
        cal_bar.addWidget(self.load_btn)
        cal_bar.addWidget(QLabel("Fit"))
        self.cal_type = QComboBox()
        self.cal_type.addItems(["Linear", "Quadratic", "Cubic"])
        self.cal_type.setCurrentText(self.config.cal_type)
        self.cal_type.currentTextChanged.connect(self._refit)
        cal_bar.addWidget(self.cal_type)
        cal_bar.addWidget(QLabel("Config"))
        self.bal_config = QComboBox()
        self.bal_config.addItems(["Force", "Moment"])
        self.bal_config.setCurrentText(self.config.balance_config)
        self.bal_config.currentTextChanged.connect(self._config_changed)
        cal_bar.addWidget(self.bal_config)
        self.cal_info = QLabel("no calibration loaded")
        self.cal_info.setObjectName("dim")
        cal_bar.addWidget(self.cal_info, 1)
        root.addLayout(cal_bar)

        self.alarm = QLabel("")
        self.alarm.setStyleSheet(
            f"background-color: {theme.ERROR}; color: white; "
            f"font-weight: bold; padding: 6px; border-radius: 4px;")
        self.alarm.setVisible(False)
        root.addWidget(self.alarm)

        # force tiles
        tiles = QHBoxLayout()
        tiles.setSpacing(6)
        self.tiles: Dict[str, _ForceTile] = {}
        for i, (name, unit) in enumerate(_FORCES):
            tile = _ForceTile(name, unit, theme.series_color(i))
            self.tiles[name] = tile
            tiles.addWidget(tile)
        root.addLayout(tiles)

        # element load-limit bars
        limits = QGroupBox("Element loads vs balance maxima")
        lg = QGridLayout(limits)
        lg.setVerticalSpacing(3)
        self.util_bars: Dict[int, QProgressBar] = {}
        self.util_labels: Dict[int, QLabel] = {}
        for i in range(6):
            lbl = QLabel(f"ch{i}")
            lbl.setFixedWidth(70)
            lg.addWidget(lbl, i % 3, (i // 3) * 3)
            bar = QProgressBar()
            bar.setRange(0, 120)
            bar.setTextVisible(False)
            bar.setFixedHeight(14)
            lg.addWidget(bar, i % 3, (i // 3) * 3 + 1)
            pct = QLabel("--")
            pct.setFixedWidth(90)
            pct.setProperty("mono", "true")
            lg.addWidget(pct, i % 3, (i // 3) * 3 + 2)
            self.util_labels[i] = lbl
            self.util_bars[i] = bar
            # stash the pct label on the bar for updates
            bar._pct = pct
        root.addWidget(limits)

        # forces history (interactive)
        self._hist = pg.PlotWidget()
        _style_plot(self._hist)
        pi = self._hist.getPlotItem()
        pi.setMenuEnabled(True)
        pi.setMouseEnabled(x=True, y=True)
        pi.setLabel("left", "force (lb) / moment (in·lb)")
        pi.setLabel("bottom", "time before now  (s)")
        pi.addLegend(offset=(8, 8), labelTextColor=theme.TEXT,
                     brush=pg.mkBrush(theme.PLOT_BG + "cc"),
                     pen=pg.mkPen(theme.BORDER))
        self._curves = {}
        for i, (name, _u) in enumerate(_FORCES):
            # width-1 non-AA pen: see plots.ChannelHistory — width-2 AA
            # streaming curves are pathologically slow to repaint embedded
            self._curves[name] = pi.plot(
                [], [], name=name, antialias=False,
                pen=pg.mkPen(theme.series_color(i), width=1))
        root.addWidget(self._hist, 1)

    # ── calibration ──
    def _browse_vol(self):
        start = str(Path(self.config.vol_path).parent) \
            if self.config.vol_path else ""
        path, _ = QFileDialog.getOpenFileName(
            self, "Load balance calibration", start,
            "Balance calibration (*.vol);;All files (*)")
        if path:
            self.load_vol(path)

    def load_vol(self, path: str) -> bool:
        try:
            cal = balcal.read_vol_file(path)
            balcal.calc_coeffs(cal, self.cal_type.currentText())
        except Exception as exc:                       # noqa: BLE001
            self.cal_info.setText(f"failed to load: {exc}")
            self.cal = None
            return False
        self.cal = cal
        self.config.vol_path = path
        self.config.cal_type = self.cal_type.currentText()
        self.config.balance_type = cal.description.balance_type
        self.config.balance_serial = cal.description.serial_number
        # auto-suggest balance config from the description
        if "moment" in cal.description.balance_type.lower():
            self.bal_config.setCurrentText("Moment")
        self.cal_info.setText(
            balcal.balance_summary(cal) + f"   [{Path(path).name}]")
        for i, name in enumerate(cal.force_channels[:6]):
            self.util_labels[i].setText(name)
            limit = cal.max_loads.values.get(name)
            self.util_bars[i]._pct.setToolTip(
                f"max {limit}" if limit else "no max-load entry")
        return True

    def _refit(self, cal_type: str):
        self.config.cal_type = cal_type
        if self.config.vol_path:
            self.load_vol(self.config.vol_path)

    def _config_changed(self, text: str):
        # Don't rename the config here — the coordinator owns the driver and
        # renames the four bridge channels (config + live ring/tare) through
        # set_balance_config, then reloads the Channels table. The combo text
        # already reflects the choice; refresh() re-syncs it to the config so
        # an external change (e.g. the Freestream Forces page) is mirrored.
        self.balanceConfigChanged.emit(text)

    def _sync_config_combo(self):
        """Reflect the config's balance layout (may change outside this
        panel) in the combo without re-emitting the change signal."""
        want = self.config.balance_config
        if want and want != self.bal_config.currentText():
            blocked = self.bal_config.blockSignals(True)
            self.bal_config.setCurrentText(want)
            self.bal_config.blockSignals(blocked)

    # ── live refresh (called at UI rate by the main panel) ──

    #: newest slice the safety path (tiles/utilization/overstress) runs on;
    #: ≥ the UI tick period so consecutive slices overlap — no sample is
    #: ever missed by the monitor.
    _SAFETY_S = 1.0
    #: cap on samples pushed through the cal pipeline for the history plot
    _PLOT_MAX = 2400

    def _wanted_fields(self):
        return ["t", "Excitation",
                *(f"{n}_V" for n in ("N1", "N2", "Y1", "Y2", "Axial", "Roll",
                                     "AftPitch", "AftYaw", "FwdPitch",
                                     "FwdYaw"))]

    @staticmethod
    def _volts_dict(data):
        return {name: data[key] for name in
                ("N1", "N2", "Y1", "Y2", "Axial", "Roll",
                 "AftPitch", "AftYaw", "FwdPitch", "FwdYaw")
                if (key := f"{name}_V") in data}

    def refresh(self, ring: Optional[ScanRingBuffer], rate_hz: float,
                window_s: float) -> None:
        # NOTE: runs even when the tab is hidden — overstress monitoring
        # must never depend on which tab is on screen. Only the history
        # plot redraw is skipped when invisible. The safety path computes
        # forces over just the newest _SAFETY_S; the full plot window goes
        # through the cal pipeline decimated and only while visible —
        # running the cubic cal over every sample in a 30 s window at the
        # UI rate is what used to bog the GUI down.
        self._sync_config_combo()          # mirror external layout changes
        if self.cal is None or ring is None:
            return
        rate = max(rate_hz, 10.0)
        visible = self.isVisible()
        n = int(window_s * rate * 1.05) + 2 if visible else \
            int(self._SAFETY_S * rate) + 2
        data = ring.tail(n, fields=self._wanted_fields())
        t = data["t"]
        if t.size < 2:
            return
        n_safe = min(t.size, int(self._SAFETY_S * rate) + 2)

        # excitation normalization — only with a live excitation reading
        # (LabVIEW's minExcitation check: normalizing by ~0 V explodes the
        # forces; below the floor, compute unnormalized and say so)
        self.exc_v = None
        exc_ok = False
        if "Excitation" in data:                # engineering volts (0-10)
            self.exc_v = float(np.median(data["Excitation"][-n_safe:]))
            exc_ok = self.exc_v >= 1.0
        raw = self._volts_dict(data)
        if len(raw) < 6:
            self.cal_info.setText("calibration loaded, but the channel "
                                  "names don't match the balance channels")
            return

        def forces_of(sel):
            r = {k: v[sel] for k, v in raw.items()}
            if exc_ok:
                r["Excitation"] = data["Excitation"][sel]
            return balcal.calc_brf_forces(
                r, self.cal, balance_config=self.bal_config.currentText())

        try:
            brf = forces_of(slice(-n_safe, None))
        except Exception as exc:                       # noqa: BLE001
            self.cal_info.setText(f"force computation failed: {exc}")
            return

        # tiles: mean of the newest ~tile window
        n_tile = max(2, int(0.2 * rate_hz))
        for name, _u in _FORCES:
            self.tiles[name].update_value(
                float(np.mean(getattr(brf, name)[-n_tile:])))
        if self.exc_v is not None and self.exc_v < 7.0:
            self.cal_info.setText(
                balcal.balance_summary(self.cal) +
                f"   ⚠ excitation {self.exc_v:.2f} V < 7 V — forces "
                f"{'UNNORMALIZED' if self.exc_v < 1.0 else 'suspect'}")

        # history plot: decimate BEFORE the cal pipeline (display-grade;
        # the safety path above watches every sample as it arrives)
        if visible:
            stride = max(1, t.size // self._PLOT_MAX)
            sel = slice(None, None, stride)
            try:
                brf_plot = forces_of(sel) if stride > 1 or \
                    t.size > n_safe else brf
            except Exception:                          # noqa: BLE001
                brf_plot = brf
            x = (t[sel] - t[-1]) if brf_plot is not brf else \
                (t[-n_safe:] - t[-1])
            for name, _u in _FORCES:
                self._curves[name].setData(x, getattr(brf_plot, name))

        # utilization / overstress (newest slice — live load state)
        util = balcal.element_utilization(self.cal, brf.elements)
        warn = self.config.warn_utilization
        worst_name, worst = "", 0.0
        for i, name in enumerate(self.cal.force_channels[:6]):
            u = util.get(name)
            bar = self.util_bars[i]
            if u is None:
                bar.setValue(0)
                bar._pct.setText("n/a")
                continue
            bar.setValue(min(int(u * 100), 120))
            bar._pct.setText(f"{u * 100:5.1f} %")
            color = theme.SUCCESS if u < warn else \
                (theme.WARNING if u < 1.0 else theme.ERROR)
            if getattr(bar, "_last_color", None) != color:
                bar._last_color = color        # restyle only on change —
                bar.setStyleSheet(              # stylesheet re-polish is slow
                    f"QProgressBar {{ background-color: {theme.BG_LIGHTER}; "
                    f"border: 1px solid {theme.BORDER}; border-radius: 3px;}}"
                    f"QProgressBar::chunk {{ background-color: {color}; "
                    f"border-radius: 2px; }}")
            if u > worst:
                worst_name, worst = name, u
        self.overstress = worst >= 1.0
        if self.overstress:
            self.alarm.setText(f"⚠ BALANCE OVERSTRESS: {worst_name} at "
                               f"{worst * 100:.0f} % of rated load")
            self.alarm.setVisible(True)
        elif worst >= warn:
            self.alarm.setText(f"⚠ approaching limit: {worst_name} at "
                               f"{worst * 100:.0f} % of rated load")
            self.alarm.setStyleSheet(
                f"background-color: {theme.WARNING}; color: black; "
                f"font-weight: bold; padding: 6px; border-radius: 4px;")
            self.alarm.setVisible(True)
        else:
            self.alarm.setVisible(False)
