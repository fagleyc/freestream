"""Forces page — monitoring LPF keeps noise from blocking runs.

Field 2026-07-24 (LSWT 50lb moment balance on the NI DAQ): raw 1 kHz
electrical noise spikes pushed the peak-based utilization over 100%,
latching the overstress record blocker and refusing runs while the
mean loads sat comfortably in range. The freestream monitor now
low-passes the raw balance volts (config.display_lpf_hz, default
10 Hz) before the live reduction — same filter the NI device app
applies. Offscreen, no hardware.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import sys
from pathlib import Path
from typing import Dict, List

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication               # noqa: E402

from freestream.config import FreestreamConfig         # noqa: E402
from freestream.hal import ChannelSpec                 # noqa: E402
from freestream.app.forces import ForcesPanel, _lowpass  # noqa: E402
from freestream.aero import load_balance_cal           # noqa: E402

CAL = Path(__file__).resolve().parents[1] / "docs" / "cal_files" \
    / "50lb 2026_07_24.vol"

CHANNELS = ["AftPitch", "AftYaw", "FwdPitch", "FwdYaw", "Axial", "Roll"]


class FakeInternalBalance:
    """Streaming fake with raw_tail (calibrated path): zero-mean noisy
    bridge volts with spikes, 5 V excitation."""

    def __init__(self, rate=1000.0, seconds=1.2):
        self.id = "ni_daq"
        self.label = "fake NI"
        self.zero_count = 0
        self.balance_config = "Moment"
        n = int(rate * seconds)
        rng = np.random.default_rng(11)
        self._raw: Dict[str, np.ndarray] = {}
        for name in CHANNELS:
            v = rng.normal(0.0, 2e-4, n)
            v[::97] += 4e-3                  # electrical spikes
            self._raw[name] = v
        self._raw["Excitation"] = np.full(n, 5.0)
        self._rate = rate

    def start(self):
        pass

    def stop(self):
        pass

    def channels(self) -> List[ChannelSpec]:
        return [ChannelSpec(name=n, unit="V", group="NI", kind="raw",
                            device_id="ni_daq")
                for n in CHANNELS + ["Excitation"]]

    def latest(self) -> Dict[str, float]:
        return {k: float(v[-1]) for k, v in self._raw.items()}

    def drain_block(self):
        return {k: v.copy() for k, v in self._raw.items()}

    def raw_tail(self, n: int) -> Dict[str, np.ndarray]:
        return {k: v[-n:] for k, v in self._raw.items()}

    def sample_rate(self) -> float:
        return self._rate


class FakeManager:
    def __init__(self, balance):
        self._balance = balance
        self.streaming = [balance]
        self.positioner = None
        self.extra_blockers = []

    def by_role(self, role):
        return self._balance if role == "balance" else None


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


def _panel(app, lpf_hz):
    bal = FakeInternalBalance()
    cfg = FreestreamConfig(operator="pytest")
    cfg.display_lpf_hz = lpf_hz
    p = ForcesPanel(FakeManager(bal), cfg)
    p.active = True
    p.cal = load_balance_cal(str(CAL))
    p._layout = "Moment"
    return p, bal


def test_lowpass_helper():
    x = np.zeros(4000)
    # spikes NOT on sample 0 — edge-hold padding holds the boundary
    # sample's level, so a spike exactly at the window edge stays visible
    x[10::97] = 1.0
    y = _lowpass(x, 1000.0, 10.0)
    assert np.max(y) < 0.15
    assert _lowpass(x, 1000.0, 0.0) is x


def test_lowpass_edge_hold_no_rolloff():
    # constant input stays constant at BOTH window ends — the old
    # convolve(mode="same") zero-padding rolled the displayed trace off
    # toward zero over the kernel half-window at each edge
    x = np.ones(4000)
    y = _lowpass(x, 1000.0, 10.0)         # 100-sample kernel
    assert y.shape == x.shape
    assert np.allclose(y, 1.0)


def test_noise_spikes_block_runs_without_lpf(app):
    p, _bal = _panel(app, lpf_hz=0.0)
    try:
        p._sample()
        assert p.overstress, "expected the raw-noise overstress symptom"
        assert p.record_blocker() is not None
    finally:
        p.shutdown()


def test_lpf_unblocks_runs_with_same_data(app):
    p, _bal = _panel(app, lpf_hz=10.0)      # the default
    try:
        p._sample()
        assert not p.overstress
        assert p.record_blocker() is None
    finally:
        p.shutdown()


def test_default_config_has_lpf_on():
    assert FreestreamConfig(operator="t").display_lpf_hz == 10.0
