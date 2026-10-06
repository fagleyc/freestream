"""Sweep engine tests — fakes + real recorder/runsheet, no hardware."""

import json
import sys
import threading
import time
from pathlib import Path

import h5py
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freestream.config import FreestreamConfig
from freestream.manager import DeviceManager
from freestream.recorder import Hdf5Recorder
from freestream.runsheet import SweepPoint, build_grid
from freestream.sweep import DONE, FAILED, SweepCallbacks, SweepEngine

FAKES = {
    "balance": {"adapter": "freestream._fakes.FakeStreamer", "enabled": True},
    "daq": {"adapter": "freestream._fakes.FakeDaq", "enabled": True},
    "pos": {"adapter": "freestream._fakes.FakePositioner", "enabled": True},
    "tun": {"adapter": "freestream._fakes.FakeTunnel", "enabled": True},
}
MODES = {"mode1": {"positioner": "pos", "balance": "balance",
                   "tunnel_conditions": "daq", "tunnel": "tun"}}


def _rig(tmp_path, **cfg_kw):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"modes": MODES, "devices": FAKES}),
                        encoding="utf-8")
    mgr = DeviceManager("mode1", sim=True, manifest_path=manifest)
    mgr.connect_all()
    for s in mgr.streaming:
        s.start()
    defaults = dict(samples=200, dwell_s=0.1, move_timeout_s=5,
                    tunnel_timeout_s=5, operator="pytest")
    defaults.update(cfg_kw)
    cfg = FreestreamConfig(**defaults)
    rec = Hdf5Recorder(tmp_path / "runs", config_name="testcfg")
    return mgr, rec, cfg


def test_five_point_alpha_sweep_writes_files(tmp_path):
    mgr, rec, cfg = _rig(tmp_path)
    events, outcomes = [], []
    engine = SweepEngine(mgr, rec, cfg, SweepCallbacks(
        on_event=events.append, on_point_done=outcomes.append))
    points = build_grid(alpha_spec="-2:1:2", dwell_s=0.05, samples=100)
    assert len(points) == 5
    results = engine.run(points)
    assert [o.status for o in results] == [DONE] * 5
    paths = [Path(o.path) for o in results]
    assert all(p.exists() for p in paths)
    with h5py.File(paths[0], "r") as f:
        assert set(f["StrainBook_0"]) == {"N1", "N2", "Y1", "Y2",
                                          "Axial", "Roll", "Excitation"}
        assert set(f["DaqBook2005"]) == {"Pdiff", "Ptot", "Temp"}
        assert "Alpha" in f["Positioner"] and "Beta" in f["Positioner"]
        assert "Time" in f["Time"]
        assert f.attrs["alpha"] == -2.0
        assert f.attrs["operator"] == "pytest"
        assert f.attrs["mode"] == "mode1"
        assert f["StrainBook_0/N1"].attrs["wf_increment"] == \
            pytest.approx(1 / 1000.0)
        assert len(f["StrainBook_0/N1"]) > 0


def test_streaming_blocks_trimmed_to_requested_samples(tmp_path):
    # each streaming group is trimmed to EXACTLY point.samples (keeping the
    # most-recent, steady-state samples); channels within a group stay
    # equal-length. A group that captured fewer keeps all it has + logs.
    mgr, rec, cfg = _rig(tmp_path)
    events = []
    engine = SweepEngine(mgr, rec, cfg,
                         SweepCallbacks(on_event=events.append))
    pt = SweepPoint(alpha=0.0, dwell_s=0.05, samples=100)
    out = engine.run([pt])[0]
    assert out.status == DONE
    with h5py.File(out.path, "r") as f:
        sb = {ch: f["StrainBook_0"][ch].shape[0] for ch in f["StrainBook_0"]}
        db = {ch: f["DaqBook2005"][ch].shape[0] for ch in f["DaqBook2005"]}
    # equal length across channels within each streaming group
    assert len(set(sb.values())) == 1, sb
    assert len(set(db.values())) == 1, db
    # StrainBook streams fast (1 kHz) so it overshoots 100 and is trimmed
    # to exactly the requested count
    assert set(sb.values()) == {100}, sb
    # DaqBook streams slower (200 Hz): it cannot reach 100 within the
    # primary-rate acquire window — an EXPECTED shortfall for a
    # rate-limited group (like the ~1 Hz Heise), demoted to a debug log:
    # no operator-facing WARNING event per point
    db_n = next(iter(db.values()))
    assert db_n <= 100
    assert not any("captured" in e and "< " in e for e in events), events


def test_runsheet_meta_inherited_into_h5(tmp_path):
    mgr, rec, cfg = _rig(tmp_path)
    engine = SweepEngine(mgr, rec, cfg)
    pt = SweepPoint(alpha=1.0, beta=0.0, dwell_s=0.05, samples=50,
                    meta={"flap_deg": 12.5, "config_name_row": "gearUp"})
    out = engine.run([pt])[0]
    assert out.status == DONE
    with h5py.File(out.path, "r") as f:
        assert f.attrs["flap_deg"] == 12.5
        assert f.attrs["config_name_row"] == "gearUp"


def test_tunnel_mach_point_and_derived_channels(tmp_path):
    # control ENABLED: the classic MachLoop command path (monitor-only
    # default is covered in test_operator_wait.py)
    mgr, rec, cfg = _rig(tmp_path, tunnel_control_enabled=True)
    events = []
    engine = SweepEngine(mgr, rec, cfg,
                         SweepCallbacks(on_event=events.append))
    pt = SweepPoint(alpha=0.0, mach=0.3, dwell_s=0.05, samples=50)
    out = engine.run([pt])[0]
    assert out.status == DONE
    # sim: measured flow can't close the loop → open-loop command, logged
    assert any("sim plant" in e and "regulation skipped" in e
               for e in events)
    # filename carries the mach token, not rpm
    assert "mach_0.30" in Path(out.path).name
    with h5py.File(out.path, "r") as f:
        assert "RPM_meas" in f["Tunnel"] and "RPM_cmd" in f["Tunnel"]
        # Mach_cmd is the constant commanded Mach; RPM_cmd the converted cmd
        assert list(f["Tunnel/Mach_cmd"][()]) == \
            pytest.approx([0.3] * f["Tunnel/Mach_cmd"].shape[0])
        assert f["Tunnel/RPM_cmd"][0] == pytest.approx(
            0.3 * cfg.rpm_per_mach)
        assert f.attrs["mach"] == 0.3
        # FakeDaq serves plausible Pdiff/Ptot/Temp → derived present
        assert "Mach_meas" in f["Tunnel"] and "q_meas" in f["Tunnel"]
        assert 0.0 < f["Tunnel/Mach_meas"][0] < 1.0


def test_rpm_override_bypasses_mach_loop(tmp_path):
    # control ENABLED: direct-RPM writes (monitor-only override behavior
    # is covered in test_operator_wait.py)
    mgr, rec, cfg = _rig(tmp_path, tunnel_control_enabled=True)
    events = []
    engine = SweepEngine(mgr, rec, cfg,
                         SweepCallbacks(on_event=events.append))
    pt = SweepPoint(alpha=0.0, dwell_s=0.05, samples=50,
                    meta={"rpm": 600.0})       # run-sheet "rpm" column
    out = engine.run([pt])[0]
    assert out.status == DONE
    assert any("direct override" in e and "Mach loop bypassed" in e
               for e in events)
    assert not any("mach loop" in e.lower() and "override" not in e
                   for e in events)
    with h5py.File(out.path, "r") as f:
        assert f["Tunnel/RPM_cmd"][0] == pytest.approx(600.0)
        assert "Mach_cmd" not in f["Tunnel"]   # no Mach was commanded
        assert f.attrs["rpm"] == 600.0         # override lands in attrs
        assert "mach" not in f.attrs


def test_refuses_to_record_with_offline_device(tmp_path):
    mgr, rec, cfg = _rig(tmp_path)
    mgr.devices["daq"].disconnect()          # pull a device offline
    engine = SweepEngine(mgr, rec, cfg)
    points = build_grid(alpha_spec="0,1,2", dwell_s=0.05, samples=50)
    results = engine.run(points)
    assert results[0].status == FAILED
    assert "REFUSING TO RECORD" in results[0].error
    # sweep paused: later points stay queued, nothing written
    assert not list((tmp_path / "runs").rglob("*.h5"))
    assert points[1].status == "queued"


def test_failed_point_rerun_individually(tmp_path):
    mgr, rec, cfg = _rig(tmp_path)
    mgr.devices["daq"].disconnect()
    engine = SweepEngine(mgr, rec, cfg)
    pt = SweepPoint(alpha=0.5, dwell_s=0.05, samples=50)
    assert engine.run([pt])[0].status == FAILED
    mgr.devices["daq"].connect()             # fix the cause
    mgr.devices["daq"].start()
    out = engine.run_point(0, pt)            # re-run JUST that point
    assert out.status == DONE and Path(out.path).exists()


def test_move_timeout_faults(tmp_path):
    mgr, rec, cfg = _rig(tmp_path, move_timeout_s=0.3)
    mgr.devices["pos"]._settle_s = 10.0      # never settles in time
    engine = SweepEngine(mgr, rec, cfg)
    out = engine.run([SweepPoint(alpha=1.0, dwell_s=0.0, samples=50)])[0]
    assert out.status == FAILED and "settled" in out.error


def test_estop_stops_motion_and_aborts(tmp_path):
    mgr, rec, cfg = _rig(tmp_path)
    mgr.devices["pos"]._settle_s = 5.0
    engine = SweepEngine(mgr, rec, cfg)
    points = build_grid(alpha_spec="0:1:8", dwell_s=0.1, samples=100)
    t = threading.Thread(target=engine.run, args=(points,), daemon=True)
    t.start()
    time.sleep(0.4)                          # mid-move
    engine.estop()
    t.join(timeout=5)
    assert not t.is_alive()
    assert mgr.devices["pos"].stopped
    assert not engine.running


# ── calibration hand-off: the active .vol travels with the run ───────────
def test_run_stages_balance_vol_beside_run_files(tmp_path):
    # F16_Val-style hand-off: run start copies the active .vol into the
    # run folder and records it in manifest.json (balance metadata riding
    # along), so Streamlined's reduction resolves it with no CalFiles
    # lookup. Nothing is applied at capture — raw stays raw.
    vol = tmp_path / "CalFiles" / "50lb 2026_07_24.vol"
    vol.parent.mkdir()
    vol.write_text("Voltage Calibration File\n", encoding="utf-8")
    mgr, rec, cfg = _rig(tmp_path, vol_path=str(vol), cal_type="Linear",
                         balance_config="Moment")
    events = []
    engine = SweepEngine(mgr, rec, cfg,
                         SweepCallbacks(on_event=events.append))
    out = engine.run([SweepPoint(alpha=0.0, dwell_s=0.05, samples=50)])[0]
    assert out.status == DONE
    copy = rec.config_dir / vol.name
    assert copy.is_file()                        # beside the run_* files
    assert copy.read_text(encoding="utf-8") == \
        vol.read_text(encoding="utf-8")
    m = json.loads(rec.manifest_path.read_text(encoding="utf-8"))
    assert m["balance_cal"]["vol_file"] == vol.name
    assert m["balance_cal"]["vol_source"] == str(vol)
    assert m["balance_cal"]["cal_type"] == "Linear"
    # DEVICE-OWNED layout wins over the config fallback (FakeStreamer
    # declares "Force"; the cfg kwarg above is deliberately different)
    assert m["balance_cal"]["balance_config"] == "Force"
    assert len(m["points"]) == 1                 # point landed after staging
    assert any("balance cal staged" in e for e in events)


def test_run_without_vol_stages_nothing(tmp_path):
    mgr, rec, cfg = _rig(tmp_path)               # no vol_path configured
    engine = SweepEngine(mgr, rec, cfg)
    out = engine.run([SweepPoint(alpha=0.0, dwell_s=0.05, samples=50)])[0]
    assert out.status == DONE
    m = json.loads(rec.manifest_path.read_text(encoding="utf-8"))
    assert "balance_cal" not in m                # additive key only
    assert not list(rec.config_dir.glob("*.vol"))
