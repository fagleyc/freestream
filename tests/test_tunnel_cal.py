"""Tunnel-conditions calibration COEFFICIENTS in the recorded per-point
file — the cal-format contract Streamlined reduces against.

The recorder writes cal_slope/cal_offset/cal_unit/cal_type ALONGSIDE each
tunnel channel's ``unit`` attr (linear for raw-volt DAQ channels, identity
for the Heise engineering values). Balance/bridge channels carry NO tunnel
cal. The RAW sample arrays are never altered (recorder's hard rule). Mirrored
in the .mat writer and produced end-to-end by a sim LSWT sweep.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "devices"))

from freestream.recorder import Hdf5Recorder, read_point  # noqa: E402

# DaqBook-style linear (raw volts × scale + offset) + Heise-style identity
LINEAR_CAL = {"Pdiff": {"slope": 0.386949, "offset": 0.0,
                        "unit": "psid", "type": "linear"},
              "Temp": {"slope": 10.0, "offset": 0.0,
                       "unit": "degC", "type": "linear"}}
IDENTITY_CAL = {"Ptot": {"slope": 1.0, "offset": 0.0,
                         "unit": "psia", "type": "identity"}}

RATES = {"DaqBook2005": 100.0, "Heise": 4.0, "StrainBook_0": 1000.0}
UNITS = {"DaqBook2005": {"Pdiff": "V", "Temp": "V"},
         "Heise": {"Ptot": "psia"},
         "StrainBook_0": {"N1": "V", "Excitation": "V"}}


def _blocks():
    return {
        "DaqBook2005": {"Pdiff": np.linspace(0.0, 1.0, 20),
                        "Temp": np.linspace(0.0, 1.0, 20)},
        "Heise": {"Ptot": np.full(20, 14.7)},
        "StrainBook_0": {"N1": np.zeros(20), "Excitation": np.zeros(20)},
    }


def _cal():
    """Flat ``{ch: cal}`` — the shape the sweep engine passes."""
    return {**LINEAR_CAL, **IDENTITY_CAL}


# ── HDF5: the four attrs on linear + identity channels ───────────────────
def test_h5_cal_attrs_linear_and_identity(tmp_path):
    rec = Hdf5Recorder(tmp_path, config_name="cal")
    path = rec.write_point(point_meta={"alpha": 0.0, "mach": 0.3},
                           blocks=_blocks(), rates=RATES,
                           channel_units=UNITS, channel_cal=_cal())
    data = read_point(path)
    ca = data["channel_attrs"]

    # DaqBook Pdiff — LINEAR, exact contract values, read back verbatim
    pd = ca["DaqBook2005"]["Pdiff"]
    assert pd["cal_slope"] == pytest.approx(0.386949)
    assert pd["cal_offset"] == 0.0
    assert pd["cal_unit"] == "psid"
    assert pd["cal_type"] == "linear"
    assert pd["unit"] == "V"                     # the raw unit still there

    temp = ca["DaqBook2005"]["Temp"]
    assert temp["cal_slope"] == pytest.approx(10.0)
    assert temp["cal_unit"] == "degC"
    assert temp["cal_type"] == "linear"

    # Heise Ptot — IDENTITY (already engineering units)
    tp = ca["Heise"]["Ptot"]
    assert tp["cal_type"] == "identity"
    assert tp["cal_slope"] == 1.0
    assert tp["cal_offset"] == 0.0
    assert tp["cal_unit"] == "psia"

    # RAW DATA VERBATIM — the sample arrays are untouched by cal metadata
    assert np.allclose(data["groups"]["DaqBook2005"]["Pdiff"],
                       np.linspace(0.0, 1.0, 20))


def test_h5_balance_channels_get_no_tunnel_cal(tmp_path):
    rec = Hdf5Recorder(tmp_path, config_name="cal")
    path = rec.write_point(point_meta={"alpha": 0.0}, blocks=_blocks(),
                           rates=RATES, channel_units=UNITS,
                           channel_cal=_cal())
    ca = read_point(path)["channel_attrs"]
    for ch in ("N1", "Excitation"):
        attrs = ca["StrainBook_0"][ch]
        for key in ("cal_slope", "cal_offset", "cal_unit", "cal_type"):
            assert key not in attrs, (ch, key)
        assert attrs["unit"] == "V"              # ordinary unit still written


def test_h5_no_cal_is_backward_compatible(tmp_path):
    rec = Hdf5Recorder(tmp_path, config_name="cal")
    path = rec.write_point(point_meta={"alpha": 0.0}, blocks=_blocks(),
                           rates=RATES, channel_units=UNITS)   # no channel_cal
    ca = read_point(path)["channel_attrs"]
    for grp in ca.values():
        for attrs in grp.values():
            assert "cal_type" not in attrs


# ── .mat mirror ──────────────────────────────────────────────────────────
def test_mat_mirror_carries_cal(tmp_path):
    pytest.importorskip("scipy")
    from scipy.io import loadmat
    rec = Hdf5Recorder(tmp_path, config_name="cal", output_format="mat")
    path = rec.write_point(point_meta={"alpha": 0.0}, blocks=_blocks(),
                           rates=RATES, channel_units=UNITS,
                           channel_cal=_cal())
    m = loadmat(str(path), struct_as_record=False, squeeze_me=True)
    chan = m["meta"].channels

    pd = chan.DaqBook2005.Pdiff
    assert float(pd.cal_slope) == pytest.approx(0.386949)
    assert str(pd.cal_unit) == "psid"
    assert str(pd.cal_type) == "linear"

    tp = chan.Heise.Ptot
    assert str(tp.cal_type) == "identity"
    assert float(tp.cal_slope) == 1.0
    assert str(tp.cal_unit) == "psia"

    # bridge channel has NO cal fields on its per-channel meta struct
    assert not hasattr(chan.StrainBook_0.N1, "cal_type")


# ── end-to-end: a sim LSWT sweep produces the correct cal_type ───────────
def _wait(cond, timeout=10.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_lswt_sim_sweep_writes_correct_cal_types(tmp_path):
    from freestream.config import FreestreamConfig
    from freestream.manager import DeviceManager
    from freestream.runsheet import build_grid
    from freestream.sweep import DONE, SweepEngine

    mgr = DeviceManager("LSWT-LSWTSting-NI", sim=True)
    errors = mgr.connect_all()
    assert errors == {}, f"sim connect failed: {errors}"
    try:
        for s in mgr.streaming:
            s.start()
        assert _wait(lambda: mgr.record_blockers() == [], 10.0), \
            mgr.record_blockers()
        cfg = FreestreamConfig(mode="LSWT-LSWTSting-NI", sim=True,
                               operator="cal", config_name="cal",
                               samples=500, dwell_s=0.1,
                               move_timeout_s=60, tunnel_timeout_s=60)
        rec = Hdf5Recorder(tmp_path / "runs", config_name=cfg.config_name)
        engine = SweepEngine(mgr, rec, cfg)
        points = build_grid(alpha_spec="0:2:2", dwell_s=0.1, samples=500)
        outcomes = engine.run(points)
        assert [o.status for o in outcomes] == [DONE, DONE], \
            [f"{o.status}:{o.error}" for o in outcomes]

        ca = read_point(outcomes[0].path)["channel_attrs"]
        # NI DAQ Pdiff — LINEAR (raw volts → psid)
        pd = ca["NI_USB_6351"]["Pdiff"]
        assert pd["cal_type"] == "linear"
        assert pd["cal_unit"] == "psid"
        assert "cal_slope" in pd and "cal_offset" in pd
        # Heise Ptot/Temp — IDENTITY (already engineering units)
        assert ca["Heise"]["Ptot"]["cal_type"] == "identity"
        assert ca["Heise"]["Ptot"]["cal_unit"] == "psia"
        assert ca["Heise"]["Temp"]["cal_type"] == "identity"
        assert ca["Heise"]["Temp"]["cal_unit"] in ("degC", "degF")
        # balance bridges carry NO tunnel cal
        for ch in ("N1", "N2", "Y1", "Y2", "Axial", "Roll"):
            assert "cal_type" not in ca["NI_USB_6351"][ch], ch
    finally:
        for s in mgr.streaming:
            s.stop()
        mgr.disconnect_all()
