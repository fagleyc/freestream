"""Output & Trigger panel — AI start trigger + analog-output control.

Trigger edits mutate ``cfg.trigger`` and are applied to the DAQmx task at
the next Connect; the state lamp mirrors the driver's armed → triggered
transition on the panel refresh tick. The AO rows edit
``cfg.ao_channels`` live: the Set button pushes a static DC level to a
connected device immediately (``device.set_ao``), and the group buttons
swap the static AO task for a regenerated waveform task.
"""

from __future__ import annotations

from typing import List

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGridLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QVBoxLayout,
    QWidget,
)

from ni_usb_6351 import theme
from ni_usb_6351.config import (
    ANALOG_SOURCES, AO_WAVEFORMS, PFI_SOURCES, TRIGGER_MODES,
    AOChannelConfig, NiDaqConfig,
)
from ni_usb_6351.device import NiUsb6351

_AO_HEADERS = ["On", "Name", "Static (V)", "", "Waveform", "Amp (V)",
               "Freq (Hz)", "Offset (V)"]


class OutputTriggerPanel(QWidget):
    """Start-trigger setup + AO static/waveform control."""

    statusSignal = pyqtSignal(str)

    def __init__(self, cfg: NiDaqConfig, device: NiUsb6351, parent=None):
        super().__init__(parent)
        self._cfg = cfg
        self._device = device
        self._ao_widgets: List[dict] = []
        self._last_state = ("", "")
        self._build()
        self._load_trigger()
        self._populate_ao()

    # ── UI ──
    def _build(self):
        root = QVBoxLayout(self)
        root.setSpacing(8)

        trig = QGroupBox("Start trigger")
        tf = QFormLayout(trig)
        self.mode_combo = QComboBox()
        self.mode_combo.addItems(list(TRIGGER_MODES))
        self.mode_combo.currentTextChanged.connect(self._mode_changed)
        tf.addRow("Mode", self.mode_combo)
        self.source_combo = QComboBox()
        self.source_combo.currentTextChanged.connect(self._source_changed)
        tf.addRow("Source", self.source_combo)
        self.edge_combo = QComboBox()
        self.edge_combo.addItems(["rising", "falling"])
        self.edge_combo.currentTextChanged.connect(
            lambda v: setattr(self._cfg.trigger, "edge", v))
        tf.addRow("Edge", self.edge_combo)
        self.level_spin = QDoubleSpinBox()
        self.level_spin.setRange(-10.0, 10.0)
        self.level_spin.setDecimals(3)
        self.level_spin.setSingleStep(0.01)
        self.level_spin.setSuffix(" V")
        self.level_spin.valueChanged.connect(
            lambda v: setattr(self._cfg.trigger, "level_v", float(v)))
        tf.addRow("Level", self.level_spin)
        self.state_lamp = QLabel("Immediate")
        self.state_lamp.setProperty("mono", "true")
        self.state_lamp.setStyleSheet(f"color: {theme.TEXT_DIM}; "
                                      f"font-weight: bold;")
        tf.addRow("State", self.state_lamp)
        hint = QLabel("Trigger changes take effect on the next Connect.")
        hint.setObjectName("dim")
        tf.addRow("", hint)
        root.addWidget(trig)

        ao = QGroupBox("Analog outputs")
        al = QVBoxLayout(ao)
        self._ao_grid = QGridLayout()
        self._ao_grid.setHorizontalSpacing(8)
        self._ao_grid.setVerticalSpacing(4)
        al.addLayout(self._ao_grid)

        bar = QHBoxLayout()
        self.start_wave_btn = QPushButton("Start waveform")
        self.start_wave_btn.setObjectName("primary")
        self.start_wave_btn.clicked.connect(self._start_wave)
        bar.addWidget(self.start_wave_btn)
        self.stop_wave_btn = QPushButton("Stop waveform")
        self.stop_wave_btn.clicked.connect(self._stop_wave)
        bar.addWidget(self.stop_wave_btn)
        self.zero_btn = QPushButton("Zero AO")
        self.zero_btn.setObjectName("danger")
        self.zero_btn.clicked.connect(self._zero_ao)
        bar.addWidget(self.zero_btn)
        bar.addStretch(1)
        al.addLayout(bar)
        ao_hint = QLabel(
            "Enable/name changes apply at the next Connect; waveform edits "
            "apply at the next Start waveform. Set pushes a static level "
            "to the live device.")
        ao_hint.setObjectName("dim")
        ao_hint.setWordWrap(True)
        al.addWidget(ao_hint)
        root.addWidget(ao)
        root.addStretch(1)

    # ── trigger ──
    def _load_trigger(self):
        trig = self._cfg.trigger
        for combo, value in ((self.mode_combo, trig.mode),
                             (self.edge_combo, trig.edge)):
            blocked = combo.blockSignals(True)
            combo.setCurrentText(value)
            combo.blockSignals(blocked)
        blocked = self.level_spin.blockSignals(True)
        self.level_spin.setValue(trig.level_v)
        self.level_spin.blockSignals(blocked)
        self._repop_sources()

    @staticmethod
    def _sources_for(mode: str):
        if mode == "digital_edge":
            return PFI_SOURCES
        if mode == "analog_edge":
            return ANALOG_SOURCES
        return ()

    def _mode_changed(self, mode: str):
        self._cfg.trigger.mode = mode
        self._repop_sources()

    def _source_changed(self, source: str):
        if source:
            self._cfg.trigger.source = source

    def _repop_sources(self):
        trig = self._cfg.trigger
        sources = self._sources_for(trig.mode)
        blocked = self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItems(list(sources))
        if trig.source in sources:
            self.source_combo.setCurrentText(trig.source)
        elif sources:
            trig.source = sources[0]
        self.source_combo.blockSignals(blocked)
        armed = trig.mode != "immediate"
        self.source_combo.setEnabled(armed)
        self.edge_combo.setEnabled(armed)
        self.level_spin.setEnabled(trig.mode == "analog_edge")

    # ── analog outputs ──
    def _populate_ao(self):
        while self._ao_grid.count():
            item = self._ao_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._ao_widgets = []
        for col, text in enumerate(_AO_HEADERS):
            lbl = QLabel(text)
            lbl.setObjectName("dim")
            self._ao_grid.addWidget(lbl, 0, col)
        for row, ch in enumerate(self._cfg.ao_channels, start=1):
            self._append_ao_row(row, ch)

    def _append_ao_row(self, row: int, ch: AOChannelConfig):
        on = QCheckBox(ch.physical)
        on.setChecked(ch.enabled)
        on.toggled.connect(lambda v, c=ch: setattr(c, "enabled", v))
        self._ao_grid.addWidget(on, row, 0)

        name = QLineEdit(ch.name)
        name.setFixedWidth(90)
        name.textChanged.connect(lambda v, c=ch: setattr(c, "name", v))
        self._ao_grid.addWidget(name, row, 1)

        static = QDoubleSpinBox()
        static.setRange(ch.v_min, ch.v_max)
        static.setDecimals(3)
        static.setSingleStep(0.001)
        static.setSuffix(" V")
        static.setValue(ch.static_v)
        self._ao_grid.addWidget(static, row, 2)

        set_btn = QPushButton("Set")
        set_btn.clicked.connect(
            lambda _=False, c=ch, sp=static: self._set_ao(c, sp))
        self._ao_grid.addWidget(set_btn, row, 3)

        wave = QComboBox()
        wave.addItems(list(AO_WAVEFORMS))
        wave.setCurrentText(ch.waveform)
        wave.currentTextChanged.connect(
            lambda v, c=ch: setattr(c, "waveform", v))
        self._ao_grid.addWidget(wave, row, 4)

        amp = QDoubleSpinBox()
        amp.setRange(0.0, 10.0)
        amp.setDecimals(3)
        amp.setSuffix(" V")
        amp.setValue(ch.amplitude_v)
        amp.valueChanged.connect(
            lambda v, c=ch: setattr(c, "amplitude_v", float(v)))
        self._ao_grid.addWidget(amp, row, 5)

        freq = QDoubleSpinBox()
        freq.setRange(0.1, 100_000.0)
        freq.setDecimals(1)
        freq.setSuffix(" Hz")
        freq.setValue(ch.freq_hz)
        freq.valueChanged.connect(
            lambda v, c=ch: setattr(c, "freq_hz", float(v)))
        self._ao_grid.addWidget(freq, row, 6)

        offs = QDoubleSpinBox()
        offs.setRange(ch.v_min, ch.v_max)
        offs.setDecimals(3)
        offs.setSuffix(" V")
        offs.setValue(ch.offset_v)
        offs.valueChanged.connect(
            lambda v, c=ch: setattr(c, "offset_v", float(v)))
        self._ao_grid.addWidget(offs, row, 7)

        self._ao_widgets.append({"cfg": ch, "on": on, "name": name,
                                 "static": static, "set": set_btn,
                                 "wave": wave, "amp": amp, "freq": freq,
                                 "offset": offs})

    def _set_ao(self, ch: AOChannelConfig, spin: QDoubleSpinBox):
        volts = float(spin.value())
        ch.static_v = ch.clamp(volts)
        if self._device.connected and ch.enabled:
            try:
                self._device.set_ao(ch.name, volts)
            except Exception as exc:                   # noqa: BLE001
                self.statusSignal.emit(f"AO set failed: {exc}")
        else:
            self.statusSignal.emit(f"AO {ch.name} = {ch.static_v:+.3f} V "
                                   f"(stored — applies at Connect)")

    def _start_wave(self):
        try:
            self._device.start_ao_wave()
        except Exception as exc:                       # noqa: BLE001
            self.statusSignal.emit(f"AO waveform failed: {exc}")

    def _stop_wave(self):
        try:
            self._device.stop_ao_wave()
        except Exception as exc:                       # noqa: BLE001
            self.statusSignal.emit(f"AO stop failed: {exc}")

    def _zero_ao(self):
        try:
            self._device.zero_ao()
        except Exception as exc:                       # noqa: BLE001
            self.statusSignal.emit(f"AO zero failed: {exc}")
        for w in self._ao_widgets:
            blocked = w["static"].blockSignals(True)
            w["static"].setValue(w["cfg"].static_v)
            w["static"].blockSignals(blocked)

    # ── refresh (called on the panel's UI tick) ──
    def refresh(self):
        dev = self._device
        trig = self._cfg.trigger
        if trig.mode == "immediate":
            state = ("Immediate", theme.TEXT_DIM)
        elif not dev.connected:
            state = (f"{trig.mode.replace('_', ' ')} on {trig.source} "
                     f"(applies at Connect)", theme.TEXT_DIM)
        elif dev.waiting_for_trigger:
            state = ("ARMED — waiting for trigger", theme.WARNING)
        elif dev.frame_count() > 0:
            state = ("Triggered — acquiring", theme.SUCCESS)
        else:
            state = ("ARMED — waiting for trigger", theme.WARNING)
        if state != self._last_state:
            self._last_state = state
            self.state_lamp.setText(state[0])
            self.state_lamp.setStyleSheet(f"color: {state[1]}; "
                                          f"font-weight: bold;")
        wave = dev.ao_wave_running
        self.start_wave_btn.setEnabled(dev.connected and not wave)
        self.stop_wave_btn.setEnabled(dev.connected and
                                      (wave or dev.sim_mode))
        self.zero_btn.setEnabled(dev.connected)

    def reload(self):
        self._load_trigger()
        self._populate_ao()
