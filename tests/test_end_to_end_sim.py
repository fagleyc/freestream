"""End-to-end acceptance (spec §10): real adapters in sim, both modes.

A 5-point alpha sweep produces per-point .h5 files that Streamlined's
new reader opens with the same access pattern as its TDMS runs.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "devices"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Streamlined"))

from freestream.config import FreestreamConfig
from freestream.manager import DeviceManager
from freestream.recorder import Hdf5Recorder
from freestream.runsheet import build_grid
from freestream.sweep import DONE, SweepEngine


def _run_sweep(tmp_path, mode: str):
    mgr = DeviceManager(mode, sim=True)
    errors = mgr.connect_all()
    assert errors == {}, f"sim connect failed: {errors}"
    try:
        for s in mgr.streaming:
            s.start()
        blockers = mgr.record_blockers()
        assert blockers == [], f"unexpected blockers: {blockers}"
        cfg = FreestreamConfig(mode=mode, sim=True, operator="e2e",
                              config_name=f"e2e_{mode}", samples=150,
                              dwell_s=0.1, move_timeout_s=60,
                              tunnel_timeout_s=60)
        rec = Hdf5Recorder(tmp_path / "runs", config_name=cfg.config_name)
        engine = SweepEngine(mgr, rec, cfg)
        points = build_grid(alpha_spec="-2:1:2", dwell_s=0.1, samples=150)
        assert len(points) == 5
        outcomes = engine.run(points)
        assert [o.status for o in outcomes] == [DONE] * 5, \
            [f"{o.status}:{o.error}" for o in outcomes]
        return [Path(o.path) for o in outcomes]
    finally:
        for s in mgr.streaming:
            s.stop()
        mgr.disconnect_all()


#: balance channel names per mode — mode1 keeps the StrainBook bridge
#: names verbatim; mode2 records the ATE's TRUE wire names
_BALANCE_CHANNELS = {
    "mode1": ("N1", "N2", "Y1", "Y2", "Axial", "Roll"),
    "mode2": ("Lift", "Pitch", "Drag", "Side", "Yaw", "Roll"),
}


@pytest.mark.parametrize("mode", ["mode1", "mode2"])
def test_five_point_alpha_sweep_streamlined_consumable(tmp_path, mode):
    paths = _run_sweep(tmp_path, mode)
    assert all(p.exists() for p in paths)

    from utils.windtunnel.data_io import read_hdf5_file
    raw, props = read_hdf5_file(str(paths[2]))    # the alpha=0 point
    # balance + tunnel + position channels, TDMS-style access
    for ch in _BALANCE_CHANNELS[mode] + ("Pdiff", "Ptot", "Temp",
                                         "Alpha", "Beta"):
        assert ch in raw.data, f"{ch} missing in {mode}"
        assert isinstance(raw.data[ch], np.ndarray)
        assert raw.data[ch].size > 0
    assert raw.time.size > 0
    # run params inherited into properties
    assert props.get("operator", raw.properties.get("operator")) == "e2e" \
        or raw.properties.get("operator") == "e2e"
    # alpha attr on the file matches the point
    assert float(raw.properties.get("alpha", props.get("alpha"))) == \
        pytest.approx(0.0)


def test_files_are_raw_only(tmp_path):
    """No coefficients, no calibrated loads — raw groups only (spec).
    Mode 1 regression: the StrainBook_0/N1.. layout is unchanged, and
    the new self-describing markers identify the sources."""
    import h5py
    paths = _run_sweep(tmp_path, "mode1")
    with h5py.File(paths[0], "r") as f:
        groups = set(f.keys())
        assert groups <= {"StrainBook_0", "DaqBook2005", "Positioner",
                          "Tunnel", "Time", "meta"}
        assert {"N1", "N2", "Y1", "Y2", "Axial", "Roll"} <= \
            set(f["StrainBook_0"])
        # cal-file POINTERS live in /meta/devices, never applied cal
        assert "meta" in groups
        # generic source markers (never hardcoded per mode)
        assert f.attrs["balance_group"] == "StrainBook_0"
        assert f.attrs["balance_type"] == "internal"
        assert f.attrs["positions_source"] == "crescent"


def test_mode2_files_reflect_the_true_device(tmp_path):
    """Mode 2 truth-naming: group ATE_Balance with the real wire names,
    NO StrainBook aliasing anywhere, /Positioner carrying the ATE's own
    streamed alpha/beta, and the self-describing meta markers."""
    import h5py
    paths = _run_sweep(tmp_path, "mode2")
    with h5py.File(paths[0], "r") as f:      # the alpha=-2 point
        assert "ATE_Balance" in f
        assert set(f["ATE_Balance"]) == {"Lift", "Pitch", "Drag", "Side",
                                         "Yaw", "Roll"}
        # no StrainBook text in ANY group name — the file reflects the
        # true device
        assert not any("StrainBook" in g for g in f.keys())
        # honest units on the resolved loads
        assert f["ATE_Balance/Lift"].attrs["unit"] == "N"
        assert f["ATE_Balance/Pitch"].attrs["unit"] == "N*m"
        # /Positioner = the ATE's own streamed positions, matching the
        # commanded point
        alpha = f["Positioner/Alpha"][()]
        beta = f["Positioner/Beta"][()]
        assert alpha.size > 0 and beta.size > 0
        assert np.allclose(alpha, -2.0, atol=0.1)
        assert np.allclose(beta, 0.0, atol=0.1)
        # self-describing markers
        assert f.attrs["balance_group"] == "ATE_Balance"
        assert f.attrs["balance_type"] == "external"
        assert f.attrs["positions_source"] == "ate"
        assert f["meta/devices/ate"].attrs["balance_type"] == "external"
