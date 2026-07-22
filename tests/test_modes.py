"""Mode 3 (traverse X/Y/Z matrix) + Custom-mode tests — real adapters in sim.

Covers the two new operating modes:
* Mode 3 builds the traverse as the Positioner PLUS the DaqBook by
  default; an automated x/y/z matrix sweep runs and records per-point
  files named by position.
* Custom mode builds from an EXPLICIT device subset and infers roles from
  capabilities; a full balance+daq+tunnel subset runs a real sim sweep;
  a tunnel-less subset rejects a Mach point clearly; the chosen set
  round-trips through FreestreamConfig.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "devices"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Streamlined"))

from freestream.config import FreestreamConfig
from freestream.hal import Positioner, SetpointDevice, Streaming
from freestream.manager import DeviceManager
from freestream.recorder import Hdf5Recorder
from freestream.runsheet import SweepPoint, build_grid
from freestream.sweep import DONE, FAILED, SweepEngine


# ── Mode 3: traverse X/Y/Z matrix + DaqBook ──────────────────────────────
def test_mode3_builds_traverse_plus_daqbook():
    mgr = DeviceManager("mode3", sim=True)
    assert set(mgr.devices) == {"traverse", "daqbook"}
    assert isinstance(mgr.positioner, Positioner)
    assert mgr.positioner.id == "traverse"
    # x/y/z inches, NOT alpha/beta
    assert {a.name for a in mgr.positioner.axes()} == {"x", "y", "z"}
    # the DaqBook records by default; still no tunnel SetpointDevice
    assert [s.id for s in mgr.streaming] == ["daqbook"]
    assert mgr.setpoint is None


def test_mode3_xyz_matrix_sweep_records(tmp_path):
    mgr = DeviceManager("mode3", sim=True)
    assert mgr.connect_all() == {}
    try:
        for s in mgr.streaming:
            s.start()
        cfg = FreestreamConfig(mode="mode3", sim=True, samples=40,
                               dwell_s=0.05, move_timeout_s=60)
        rec = Hdf5Recorder(tmp_path / "runs", config_name="mode3")
        engine = SweepEngine(mgr, rec, cfg)
        # y/z targets sit inside the current soft limits (ranges start
        # at the −18" homing datum; spans are placeholders — traverse
        # README TODO)
        points = build_grid(x_spec="0:2:4", y_spec="-17.5",
                            z_spec="-17.9", dwell_s=0.05, samples=40)
        assert len(points) == 3
        # x varies fastest (innermost) across the matrix
        assert [p.x for p in points] == [0.0, 2.0, 4.0]
        outcomes = engine.run(points)
        assert [o.status for o in outcomes] == [DONE] * 3, \
            [f"{o.status}:{o.error}" for o in outcomes]
        # per-point files are named by traverse position
        names = [Path(o.path).name for o in outcomes]
        assert all("x_" in n and "y_-17.5" in n and "z_-17.9" in n
                   for n in names), names
        # the positioner block is X/Y/Z in inches
        data = Hdf5Recorder.read_point(outcomes[0].path)
        assert set(data["groups"]["Positioner"]) == {"X", "Y", "Z"}
        assert data["channel_attrs"]["Positioner"]["X"]["unit"] == "in"
        assert data["attrs"]["x"] == 0.0 and data["attrs"]["z"] == -17.9
    finally:
        for s in mgr.streaming:
            s.stop()
        mgr.disconnect_all()


def test_streaming_less_set_still_refused_clearly(tmp_path):
    """A device set with NO data devices (custom traverse-only) still
    refuses an automated sweep with the clear message."""
    mgr = DeviceManager.custom(["traverse"], sim=True)
    assert mgr.connect_all() == {}
    cfg = FreestreamConfig(sim=True, samples=50, dwell_s=0.0,
                           move_timeout_s=5)
    rec = Hdf5Recorder(tmp_path / "runs", config_name="trav")
    engine = SweepEngine(mgr, rec, cfg)
    out = engine.run_point(0, SweepPoint(x=1.0, samples=50))
    assert out.status == FAILED
    assert "no data devices to record" in out.error
    # nothing was written
    assert not list((tmp_path / "runs").rglob("*.h5"))
    mgr.disconnect_all()


# ── Custom mode: pick devices one by one ─────────────────────────────────
def test_custom_infers_roles_from_capabilities():
    mgr = DeviceManager.custom(["crescent", "daqbook"], sim=True)
    assert set(mgr.devices) == {"crescent", "daqbook"}
    assert mgr.mode == "custom"
    assert isinstance(mgr.positioner, Positioner)
    assert mgr.positioner.id == "crescent"
    assert [s.id for s in mgr.streaming] == ["daqbook"]
    assert all(isinstance(s, Streaming) for s in mgr.streaming)
    assert mgr.setpoint is None                 # no SetpointDevice chosen


def test_custom_with_tunnel_infers_setpoint():
    mgr = DeviceManager.custom(
        ["crescent", "strainbook", "daqbook", "tunnel"], sim=True)
    assert isinstance(mgr.setpoint, SetpointDevice)
    assert mgr.setpoint.id == "tunnel"
    assert mgr.positioner.id == "crescent"
    assert {s.id for s in mgr.streaming} == {"strainbook", "daqbook"}


def test_custom_full_set_runs_sim_sweep(tmp_path):
    mgr = DeviceManager.custom(
        ["crescent", "strainbook", "daqbook", "tunnel"], sim=True)
    assert mgr.connect_all() == {}
    try:
        for s in mgr.streaming:
            s.start()
        assert mgr.record_blockers() == []
        cfg = FreestreamConfig(mode="custom",
                               custom_devices=["crescent", "strainbook",
                                               "daqbook", "tunnel"],
                               sim=True, operator="modes", config_name="cust",
                               samples=120, dwell_s=0.1, move_timeout_s=60,
                               tunnel_timeout_s=60)
        rec = Hdf5Recorder(tmp_path / "runs", config_name=cfg.config_name)
        engine = SweepEngine(mgr, rec, cfg)
        points = build_grid(alpha_spec="-1:1:0", dwell_s=0.1, samples=120)
        assert len(points) == 2
        outcomes = engine.run(points)
        assert [o.status for o in outcomes] == [DONE, DONE], \
            [f"{o.status}:{o.error}" for o in outcomes]
        assert all(Path(o.path).exists() for o in outcomes)
    finally:
        for s in mgr.streaming:
            s.stop()
        mgr.disconnect_all()


def test_custom_without_tunnel_rejects_mach_point(tmp_path):
    mgr = DeviceManager.custom(["crescent", "daqbook"], sim=True)
    assert mgr.connect_all() == {}
    try:
        for s in mgr.streaming:
            s.start()
        cfg = FreestreamConfig(mode="custom", sim=True, samples=50,
                               dwell_s=0.0, move_timeout_s=5)
        rec = Hdf5Recorder(tmp_path / "runs", config_name="cust")
        engine = SweepEngine(mgr, rec, cfg)
        out = engine.run_point(0, SweepPoint(alpha=0.0, mach=0.3, samples=50))
        assert out.status == FAILED
        assert "no tunnel SetpointDevice" in out.error
        assert not list((tmp_path / "runs").rglob("*.h5"))
    finally:
        for s in mgr.streaming:
            s.stop()
        mgr.disconnect_all()


def test_custom_device_list_round_trips_config(tmp_path):
    chosen = ["crescent", "strainbook", "daqbook", "tunnel"]
    cfg = FreestreamConfig(mode="custom", custom_devices=chosen)
    path = tmp_path / "cfg.json"
    cfg.save(path)
    loaded = FreestreamConfig.load(path)
    assert loaded.mode == "custom"
    assert loaded.custom_devices == chosen
