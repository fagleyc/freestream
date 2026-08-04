"""Forces panel: moment-balance limits, display LPF, excitation floor.

Field case 2026-07-24 (LSWT 50lb moment balance, 5 V excitation):
utilization bars read over-range while displayed loads were in range
(raw 1 kHz noise peaks vs mean tiles), and the hardcoded 7 V excitation
floor false-alarmed on the 5 V supply.
"""

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from ni_usb_6351 import balcal
from ni_usb_6351.app.forces_panel import ForcesPanel
from ni_usb_6351.app.plots import lowpass
from ni_usb_6351.config import NiDaqConfig
from ni_usb_6351.datamodel import ScanRingBuffer, fields_for

CAL = Path(__file__).resolve().parents[2] / "docs" / "cal_files" \
    / "50lb 2026_07_24.vol"

MOMENT_CHANNELS = ["AftPitch", "AftYaw", "FwdPitch", "FwdYaw",
                   "Axial", "Roll", "Excitation"]


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([sys.argv[0]])


def _ring_with(volts_fn, seconds=2.0, rate=1000.0, exc=5.0):
    """Ring filled with synthetic moment-bridge volts."""
    ring = ScanRingBuffer(fields_for(MOMENT_CHANNELS))
    n = int(seconds * rate)
    t = np.arange(n) / rate
    block = {"t": t}
    for name in MOMENT_CHANNELS:
        v = (np.full(n, exc) if name == "Excitation"
             else volts_fn(name, t))
        block[name] = v
        block[f"{name}_V"] = v
    ring.push_block(block)
    return ring


def _panel(qapp, lpf=10.0, min_exc=4.0):
    cfg = NiDaqConfig(balance_config="Moment")
    cfg.display_lpf_hz = lpf
    cfg.min_excitation_v = min_exc
    p = ForcesPanel(cfg)
    assert CAL.exists(), CAL
    assert p.load_vol(str(CAL))
    p.bal_config.setCurrentText("Moment")
    return p


def test_lowpass_kills_noise_peaks():
    rng = np.random.default_rng(1)
    x = rng.normal(0.0, 1.0, 10_000)
    y = lowpass(x, 1000.0, 10.0)
    assert np.max(np.abs(y)) < 0.25 * np.max(np.abs(x))
    assert lowpass(x, 1000.0, 0.0) is x          # 0 = off


def test_config_fields_defaults():
    cfg = NiDaqConfig()
    assert cfg.display_lpf_hz == 10.0
    assert cfg.min_excitation_v == 4.0


def test_limits_extracted_from_50lb_cal():
    """The rated maxima come through name-exact from the .vol."""
    cal = balcal.read_vol_file(str(CAL))
    assert cal.max_loads.values == {
        "Aft_Pitch": 75.0, "Aft_Yaw": 50.0, "Fwd_Pitch": 75.0,
        "Fwd_Yaw": 50.0, "Ax": 25.0, "Mx": 25.0}
    # every element resolves a limit (no fuzzy-match misses)
    util = balcal.element_utilization(cal, cal.force)
    assert set(util) == set(cal.force_channels)


def test_no_false_excitation_warning_at_5v(qapp):
    p = _panel(qapp)
    ring = _ring_with(lambda name, t: np.zeros_like(t), exc=5.0)
    p.refresh(ring, 1000.0, 2.0)
    assert p.exc_v == pytest.approx(5.0)
    assert "excitation" not in p.cal_info.text().lower()


def test_low_excitation_still_warns(qapp):
    p = _panel(qapp)
    ring = _ring_with(lambda name, t: np.zeros_like(t), exc=3.0)
    p.refresh(ring, 1000.0, 2.0)
    assert "excitation 3.00" in p.cal_info.text()


def test_noise_peaks_no_longer_inflate_utilization(qapp):
    """Zero mean load + electrical noise spikes: with the display LPF
    the utilization bars must stay low; unfiltered they read high (the
    reported over-range-while-in-range symptom)."""
    rng = np.random.default_rng(7)

    def noisy(name, t):
        v = rng.normal(0.0, 2e-4, t.size)      # broadband noise
        v[::97] += 4e-3                        # spikes
        return v

    def bars(panel):
        return {i: b.value() for i, b in panel.util_bars.items()}

    p_raw = _panel(qapp, lpf=0.0)
    p_raw.refresh(_ring_with(noisy), 1000.0, 2.0)
    rng = np.random.default_rng(7)
    p_flt = _panel(qapp, lpf=10.0)
    p_flt.refresh(_ring_with(noisy), 1000.0, 2.0)

    raw_worst = max(bars(p_raw).values())
    flt_worst = max(bars(p_flt).values())
    assert flt_worst < raw_worst
    assert flt_worst <= 20                     # in-range with LPF on
