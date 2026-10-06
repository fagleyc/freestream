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
from ni_usb_6351.device import (_AGGREGATE_MAX, _decimate,
                                _ghost_estimate, effective_oversample)


def test_default_bridge_ranges_are_tightest():
    for ch in default_channels("Force")[:6]:
        assert ch.native_range == 0.1, ch.name
    for ch in default_channels("Moment")[:6]:
        assert ch.native_range == 0.1, ch.name
    assert default_channels("Force")[6].native_range == 10.0  # Excitation


def test_oversample_default_and_round_trip(tmp_path):
    cfg = NiDaqConfig()
    assert cfg.oversample == 0     # 0 = AUTO: fill the aggregate budget
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


def test_effective_oversample_auto_fills_budget():
    """AUTO (0) uses the whole 1 MS/s aggregate: the rig's 7 channels
    at 1 kHz get 142x (994 kS/s), ~12x noise reduction on the
    uncorrelated floor."""
    assert effective_oversample(0, 1000.0, 7) == 142
    assert 1000.0 * 142 * 7 <= _AGGREGATE_MAX
    # one channel at 1 kHz: 1000x fits inside the aggregate
    assert effective_oversample(0, 1000.0, 1) == 1000
    # rates that already saturate the budget degrade gracefully to 1x
    assert effective_oversample(0, 200_000.0, 8) == 1


def test_mux_settle_reread_default_and_round_trip(tmp_path):
    """The mux settling guard defaults ON (F16_Val: Pdiff on AI7
    ghosted +0.45 mV into Aft_Pitch on AI0 across the scan wrap) and
    round-trips through save/load."""
    cfg = NiDaqConfig()
    assert cfg.mux_settle_reread is True
    cfg.mux_settle_reread = False
    p = tmp_path / "cfg.json"
    cfg.save(p)
    assert NiDaqConfig.load(p).mux_settle_reread is False


def test_ghost_estimate_measures_absorber_residual():
    """The absorber pre-read minus the kept read of the same channel is
    the live mux-ghost measurement: exact when the residual is constant,
    ~0 when the front end settles."""
    rows = np.zeros((3, 100))
    rows[0] = 450e-6          # absorber read of N1 still carries ghost
    rows[1] = 0.0             # kept N1 read, settled
    rows[2] = 5.0             # unrelated channel
    est = _ghost_estimate(rows, [(0, 1, "N1")])
    assert est["N1"] == pytest.approx(450e-6)
    est0 = _ghost_estimate(np.zeros((2, 10)), [(0, 1, "N1")])
    assert est0["N1"] == 0.0


def test_tare_values_exposed_and_baked_into_stream():
    """The recorded <name>_V stream is tare-subtracted, so the active
    tare MUST be inspectable for run metadata (raw = recorded+tare)."""
    import time as _time
    from ni_usb_6351.device import NiUsb6351
    dev = NiUsb6351(NiDaqConfig(force_sim=True, scan_hz=500.0))
    try:
        dev.connect()
        dev.start()
        deadline = _time.perf_counter() + 5.0
        while (_time.perf_counter() < deadline
               and dev.frame_count() < 100):
            _time.sleep(0.02)
        assert dev.tare_values == {}
        tare = dev.tare(seconds=0.1)
        assert dev.tare_values == tare
        assert set(tare) == {"N1", "N2", "Y1", "Y2", "Axial", "Roll"}
        dev.clear_tare()
        assert dev.tare_values == {}
    finally:
        dev.disconnect()


def test_effective_oversample_clamps_requests():
    """An explicit request is honoured when it fits and clamped to the
    aggregate budget when it doesn't."""
    assert effective_oversample(16, 1000.0, 7) == 16
    assert effective_oversample(64, 10_000.0, 8) == 12   # 5.12 MS/s ask
    assert 10_000.0 * 12 * 8 <= _AGGREGATE_MAX
    assert effective_oversample(1, 1000.0, 7) == 1       # averaging off
    assert effective_oversample(4, 0.0, 7) == 1          # degenerate
