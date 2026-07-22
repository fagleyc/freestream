"""LSWT-LSWTSting-NI mode + full-catalog Custom mode — real adapters in
sim.

* Custom mode offers/builds EVERY device in the manifest registry, and
  capability inference classifies the four LSWT-era devices correctly
  (lswt_sting → positioner, ni_daq → balance, heise →
  tunnel_conditions, lswt → tunnel).
* A sim alpha sweep in the new mode writes self-describing files with
  the generic role-derived markers (no per-mode hardcoding): balance
  group = the NI adapter's group, balance_type internal,
  positions_source lswt_sting, /Positioner Alpha/Beta from the sting,
  /Tunnel from the LSWT fan readback.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "devices"))

from freestream.config import FreestreamConfig
from freestream.manager import DEFAULT_MANIFEST, DeviceManager
from freestream.recorder import Hdf5Recorder
from freestream.runsheet import build_grid
from freestream.sweep import DONE, SweepEngine

LSWT_SET = ["lswt_sting", "ni_daq", "heise", "lswt"]


# ── Custom mode covers the WHOLE device folder ───────────────────────────
def test_custom_builds_every_registered_device():
    all_ids = list(json.loads(
        DEFAULT_MANIFEST.read_text(encoding="utf-8"))["devices"])
    assert {"crescent", "strainbook", "daqbook", "ate", "tunnel",
            "traverse", "lswt_sting", "ni_daq", "heise", "lswt"} \
        == set(all_ids)
    mgr = DeviceManager.custom(all_ids, sim=True)
    assert set(mgr.devices) == set(all_ids)
    # every role got claimed by SOMETHING
    for role in ("positioner", "balance", "tunnel_conditions", "tunnel"):
        assert role in mgr.roles, mgr.roles


def test_custom_infers_roles_for_the_lswt_devices():
    mgr = DeviceManager.custom(LSWT_SET, sim=True)
    assert mgr.mode == DeviceManager.CUSTOM
    assert mgr.roles["positioner"] == "lswt_sting"
    assert mgr.roles["balance"] == "ni_daq"
    assert mgr.roles["tunnel_conditions"] == "heise"
    assert mgr.roles["tunnel"] == "lswt"


# ── data modularity: the sweep's generic markers ─────────────────────────
def _wait(cond, timeout=10.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_lswt_sim_sweep_writes_self_describing_files(tmp_path):
    import h5py
    mgr = DeviceManager("LSWT-LSWTSting-NI", sim=True)
    errors = mgr.connect_all()
    assert errors == {}, f"sim connect failed: {errors}"
    try:
        for s in mgr.streaming:
            s.start()
        # let the slow Heise poll + LSWT drive poll produce first samples
        assert _wait(lambda: mgr.record_blockers() == [], 10.0), \
            mgr.record_blockers()
        cfg = FreestreamConfig(mode="LSWT-LSWTSting-NI", sim=True,
                               operator="lswt", config_name="lswt",
                               samples=1000, dwell_s=0.1,
                               move_timeout_s=60, tunnel_timeout_s=60)
        rec = Hdf5Recorder(tmp_path / "runs", config_name=cfg.config_name)
        engine = SweepEngine(mgr, rec, cfg)
        points = build_grid(alpha_spec="0:2:2", dwell_s=0.1, samples=1000)
        assert len(points) == 2
        outcomes = engine.run(points)
        assert [o.status for o in outcomes] == [DONE, DONE], \
            [f"{o.status}:{o.error}" for o in outcomes]

        with h5py.File(outcomes[1].path, "r") as f:      # the alpha=2 point
            # generic role-derived markers — never hardcoded per mode
            assert f.attrs["mode"] == "LSWT-LSWTSting-NI"
            assert f.attrs["positions_source"] == "lswt_sting"
            assert f.attrs["balance_group"] == "NI_USB_6351"
            assert f.attrs["balance_type"] == "internal"
            # the NI group carries the bridges + Pdiff, raw
            assert "NI_USB_6351" in f
            assert {"N1", "N2", "Y1", "Y2", "Axial", "Roll",
                    "Pdiff"} <= set(f["NI_USB_6351"])
            # the Heise group carries Ptot/Temp engineering samples
            assert set(f["Heise"]) >= {"Ptot", "Temp"}
            assert f["Heise/Ptot"].shape[0] > 0
            # /Positioner: the sting's Alpha/Beta at the commanded point
            import numpy as np
            alpha = f["Positioner/Alpha"][()]
            assert alpha.size > 0
            assert np.allclose(alpha, 2.0, atol=0.2), alpha
            assert "Beta" in f["Positioner"]
            # /Tunnel: engine-written channels from the fan readback
            assert {"RPM_meas", "RPM_cmd"} <= set(f["Tunnel"])
    finally:
        for s in mgr.streaming:
            s.stop()
        mgr.disconnect_all()
