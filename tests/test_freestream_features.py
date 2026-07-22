"""Tests for the Freestream feature additions: aero reduction, hysteresis
direction tagging, filename convention, device-config bundling, the live
Forces overstress interlock, and the Results polar ingest."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import sys
import tempfile
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freestream import aero                             # noqa: E402
from freestream.config import FreestreamConfig          # noqa: E402
from freestream.manager import DeviceManager            # noqa: E402
from freestream.recorder import Hdf5Recorder            # noqa: E402
from freestream.runsheet import build_grid              # noqa: E402

_CALFILES = Path(__file__).resolve().parents[2] / "Streamlined" / "CalFiles"
_FORCE_VOL = _CALFILES / "2025_06_06_2 100 lb.vol"


# ── aero reduction ────────────────────────────────────────────────────────
def test_load_cal_and_compute_lift_drag():
    cal = aero.load_balance_cal(str(_FORCE_VOL), "Linear")
    assert cal.coeffs.size > 0
    n = 20
    raw = {c: 1e-3 * np.ones(n) for c in aero.FORCE_CHANNELS}
    raw["Excitation"] = 10.0 * np.ones(n)
    geom = aero.Geometry(S=100.0, c=10.0, b=20.0)
    res = aero.compute_aero(raw, cal, alpha_deg=5.0, beta_deg=0.0,
                            balance_config="Force", q=0.44, geom=geom)
    m = res.means()
    for key in ("Lift", "Drag", "Side", "Pitch", "CL", "CD"):
        assert key in m and np.isfinite(m[key])
    assert set(res.utilization) <= set(cal.force_channels[:6])


def test_wind_axis_matches_closed_form():
    body = {"Fx": np.array([2.0]), "Fy": np.array([0.5]),
            "Fz": np.array([10.0]), "Mx": np.array([1.0]),
            "My": np.array([2.0]), "Mz": np.array([3.0])}
    a, b = 8.0, 3.0
    w = aero.wind_axis(body, a, b)
    ar, br = np.deg2rad(a), np.deg2rad(b)
    lift = np.cos(ar) * 10.0 - np.sin(ar) * 2.0
    assert w["Lift"][0] == pytest.approx(lift)
    assert w["Pitch"][0] == pytest.approx(2.0)   # moments carry through


def test_overstress_flag_when_element_over_limit():
    cal = aero.load_balance_cal(str(_FORCE_VOL), "Linear")
    # huge volts → elements blow past the rated maxima
    raw = {c: 1.0 * np.ones(5) for c in aero.FORCE_CHANNELS}
    raw["Excitation"] = 10.0 * np.ones(5)
    res = aero.compute_aero(raw, cal, 0.0, 0.0, "Force")
    assert res.overstress is True
    assert res.worst_util >= 1.0


# ── hysteresis direction tagging ──────────────────────────────────────────
def test_return_sweep_tags_up_and_down_legs():
    pts = build_grid(alpha_spec="0:2:4R", beta_spec="0", dwell_s=0.0)
    assert [p.alpha for p in pts] == [0, 2, 4, 2, 0]
    assert [p.direction for p in pts] == ["up", "up", "up", "dn", "dn"]
    # the repeated alpha=2 carries opposite alpha_dot on each leg
    up2 = next(p for p in pts if p.alpha == 2 and p.direction == "up")
    dn2 = next(p for p in pts if p.alpha == 2 and p.direction == "dn")
    assert up2.meta["alpha_dot"] == 1 and dn2.meta["alpha_dot"] == -1


def test_monotonic_sweep_has_no_direction_tag():
    pts = build_grid(alpha_spec="0:2:4", beta_spec="0", dwell_s=0.0)
    assert all(p.direction == "" for p in pts)
    assert all("sweep_dir" not in p.meta for p in pts)


# ── filename convention ───────────────────────────────────────────────────
def test_filename_includes_direction_and_no_collision():
    rec = Hdf5Recorder(tempfile.mkdtemp(), "hyst")
    up = rec.filename_for(2, {"alpha": 2.0, "beta": 0.0, "sweep_dir": "up"})
    dn = rec.filename_for(4, {"alpha": 2.0, "beta": 0.0, "sweep_dir": "dn"})
    # direction is the trailing token now that air_state left the filename
    assert up.endswith("_up.h5") and dn.endswith("_dn.h5") and up != dn


def test_filename_template_render():
    rec = Hdf5Recorder(tempfile.mkdtemp(), "cfg",
                       filename_template="{config_name}_r{run}_a{alpha}_{dir}")
    name = rec.filename_for(7, {"alpha": -2.0, "sweep_dir": "dn"}, "AirOn")
    assert name == "cfg_r0007_a-2.0_dn.h5"


def test_direction_lands_in_hdf5_attrs():
    rec = Hdf5Recorder(tempfile.mkdtemp(), "attrs")
    blocks = {"StrainBook_0": {"N1": np.arange(10.0)}}
    path = rec.write_point(point_meta={"alpha": 2.0, "sweep_dir": "dn",
                                       "alpha_dot": -1},
                           blocks=blocks, rates={"StrainBook_0": 100.0})
    from freestream.recorder import read_point
    data = read_point(path)
    assert data["attrs"]["sweep_dir"] == "dn"
    assert data["attrs"]["alpha_dot"] == -1


# ── device-config bundling ────────────────────────────────────────────────
def test_every_adapter_config_roundtrips_in_place():
    mgr = DeviceManager("mode1", sim=True)
    for dev in mgr.devices.values():
        d = dev.config_dict()
        assert isinstance(d, dict) and d
        obj = dev.config           # same object the driver holds
        dev.apply_config_dict(d)   # must not swap the object out
        assert dev.config is obj
        assert dev.has_settings() and ":" in dev.settings_dialog_path


def test_extra_blockers_feed_record_interlock():
    mgr = DeviceManager("mode1", sim=True)
    mgr.connect_all()
    for s in mgr.streaming:
        s.start()
    assert mgr.record_blockers() == []          # all sim devices OK
    mgr.extra_blockers.append(lambda: "BALANCE OVERSTRESS — test")
    assert any("OVERSTRESS" in b for b in mgr.record_blockers())
    mgr.extra_blockers.append(lambda: None)     # None hooks are ignored
    assert sum("OVERSTRESS" in b for b in mgr.record_blockers()) == 1
