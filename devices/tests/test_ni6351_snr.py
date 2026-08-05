"""NI 6351 SNR measures: tight bridge ranges + oversample decimation.

The LSWT 50lb moment balance produces 2.4-5.8 mV full-scale signals
(33-233 uV per unit load at 5 V excitation). The 6351 has no
instrument amp and no analog anti-alias filter, so per-sample SNR is
range- and bandwidth-limited: the tightest ±0.1 V range and hardware
oversampling with mean decimation are the only honest levers.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ni_usb_6351.config import NiDaqConfig, default_channels
from ni_usb_6351.device import _AGGREGATE_MAX, _decimate


def test_default_bridge_ranges_are_tightest():
    for ch in default_channels("Force")[:6]:
        assert ch.native_range == 0.1, ch.name
    for ch in default_channels("Moment")[:6]:
        assert ch.native_range == 0.1, ch.name
    assert default_channels("Force")[6].native_range == 10.0  # Excitation


def test_oversample_default_and_round_trip(tmp_path):
    cfg = NiDaqConfig()
    assert cfg.oversample == 16
    cfg.oversample = 8
    p = tmp_path / "cfg.json"
    cfg.save(p)
    assert NiDaqConfig.load(p).oversample == 8


def test_decimate_exact_means():
    data = np.arange(24, dtype=float).reshape(2, 12)
    out, carry = _decimate(None, data, 4)
    assert out.shape == (2, 3)
    assert np.allclose(out[0], [1.5, 5.5, 9.5])
    assert carry is None


def test_decimate_carry_across_calls():
    """Chunks that don't divide evenly must carry into the next read —
    the average groups stay exact across block boundaries."""
    data = np.arange(20, dtype=float).reshape(1, 20)
    out1, carry = _decimate(None, data[:, :7], 4)      # 7 → 1 group + 3
    assert out1.shape == (1, 1) and carry.shape == (1, 3)
    out2, carry = _decimate(carry, data[:, 7:10], 4)   # 3+3=6 → 1 + 2
    out3, carry = _decimate(carry, data[:, 10:20], 4)  # 2+10=12 → 3 + 0
    got = np.concatenate([out1, out2, out3], axis=1)
    want, _ = _decimate(None, data, 4)
    assert np.allclose(got, want)
    assert carry is None


def test_decimate_noise_reduction():
    rng = np.random.default_rng(3)
    x = rng.normal(0.0, 1.0, (1, 64000))
    out, _ = _decimate(None, x, 16)
    ratio = np.std(out) / np.std(x)
    assert ratio == pytest.approx(1 / 4, rel=0.15)     # sqrt(16)


def test_decimate_passthrough_at_1():
    data = np.ones((2, 5))
    out, carry = _decimate(None, data, 1)
    assert out is data and carry is None


def test_aggregate_clamp_math():
    """7 ch x 1 kHz x 16 = 112 kS/s — well under the 1 MS/s aggregate;
    a pathological request must be halved until it fits."""
    assert 7 * 1000.0 * 16 < _AGGREGATE_MAX
    os_ = 64
    n_ch, scan = 8, 10_000.0            # 5.12 MS/s requested
    while os_ > 1 and scan * os_ * n_ch > _AGGREGATE_MAX:
        os_ //= 2
    assert scan * os_ * n_ch <= _AGGREGATE_MAX
    assert os_ == 8
