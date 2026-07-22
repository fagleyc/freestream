"""Mach-detection toggle (config.mach_check_enabled).

True (default): per-point Mach gate as before — monitor-only mach/rpm
points raise the operator wait. False: the gate is SKIPPED entirely —
no operator MachWaitDialog, no settle wait; each point records
immediately after positioning with the tunnel channels still recorded.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import json
import sys
from pathlib import Path

import h5py
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freestream.config import FreestreamConfig
from freestream.manager import DeviceManager
from freestream.recorder import Hdf5Recorder
from freestream.runsheet import SweepPoint
from freestream.sweep import DONE, SweepCallbacks, SweepEngine

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
    defaults = dict(samples=100, dwell_s=0.05, move_timeout_s=5,
                    tunnel_timeout_s=5, operator="pytest")
    defaults.update(cfg_kw)
    cfg = FreestreamConfig(**defaults)
    rec = Hdf5Recorder(tmp_path / "runs", config_name="testcfg")
    return mgr, rec, cfg


# ── config ───────────────────────────────────────────────────────────────
def test_config_default_and_json_roundtrip(tmp_path):
    cfg = FreestreamConfig()
    assert cfg.mach_check_enabled is True          # default: verify
    cfg.mach_check_enabled = False
    path = tmp_path / "cfg.json"
    cfg.save(path)
    assert FreestreamConfig.load(path).mach_check_enabled is False
    # old configs without the key load with the default
    path.write_text(json.dumps({"operator": "casey"}), encoding="utf-8")
    assert FreestreamConfig.load(path).mach_check_enabled is True


# ── engine: gate skipped entirely when disabled ──────────────────────────
def test_disabled_never_raises_operator_wait(tmp_path):
    mgr, rec, cfg = _rig(tmp_path, mach_check_enabled=False)
    assert cfg.tunnel_control_enabled is False     # monitor-only default
    waits, events = [], []
    calls = []
    tun = mgr.devices["tun"]
    orig = tun.set_target
    tun.set_target = lambda **kw: (calls.append(kw), orig(**kw))
    engine = SweepEngine(mgr, rec, cfg, SweepCallbacks(
        on_event=events.append,
        on_operator_wait=lambda req: waits.append(req) or "proceed"))
    points = [SweepPoint(alpha=0.0, mach=0.0, dwell_s=0.05, samples=50),
              SweepPoint(alpha=0.0, mach=0.3, dwell_s=0.05, samples=50)]
    results = engine.run(points)
    assert [r.status for r in results] == [DONE, DONE]
    assert waits == []                             # NO operator wait raised
    assert calls == []                             # fan never commanded
    assert any("Mach verification disabled" in e for e in events)
    # tunnel channels still recorded: requested Mach_cmd + honest meas
    with h5py.File(results[1].path, "r") as f:
        assert list(f["Tunnel/Mach_cmd"][()]) == \
            pytest.approx([0.3] * f["Tunnel/Mach_cmd"].shape[0])
        assert "RPM_meas" in f["Tunnel"] and "Mach_meas" in f["Tunnel"]
        assert f.attrs["mach"] == 0.3
    # air-off Mach-0 point logic unchanged: mach 0 → AirOff
    with h5py.File(results[0].path, "r") as f:
        assert f.attrs["air_state"] == "AirOff"
    with h5py.File(results[1].path, "r") as f:
        assert f.attrs["air_state"] == "AirOn"


def test_disabled_rpm_override_also_skips_wait(tmp_path):
    mgr, rec, cfg = _rig(tmp_path, mach_check_enabled=False)
    waits = []
    engine = SweepEngine(mgr, rec, cfg, SweepCallbacks(
        on_operator_wait=lambda req: waits.append(req) or "proceed"))
    pt = SweepPoint(alpha=0.0, dwell_s=0.05, samples=50,
                    meta={"rpm": 600.0})
    out = engine.run([pt])[0]
    assert out.status == DONE
    assert waits == []
    with h5py.File(out.path, "r") as f:
        assert f["Tunnel/RPM_cmd"][0] == pytest.approx(600.0)  # requested
        assert "Mach_cmd" not in f["Tunnel"]


def test_enabled_keeps_operator_wait(tmp_path):
    mgr, rec, cfg = _rig(tmp_path)                 # mach_check default True
    waits = []
    engine = SweepEngine(mgr, rec, cfg, SweepCallbacks(
        on_operator_wait=lambda req: waits.append(req) or "proceed"))
    pt = SweepPoint(alpha=0.0, mach=0.3, dwell_s=0.05, samples=50)
    assert engine.run([pt])[0].status == DONE
    assert len(waits) == 1                         # gate still raised


# ═══ setup dialog ════════════════════════════════════════════════════════
from PyQt6.QtWidgets import QApplication, QCheckBox    # noqa: E402

from freestream.app.setup_dialog import MeasurementSetupDialog  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


def test_checkbox_lives_in_tunnel_group_next_to_control(app):
    dlg = MeasurementSetupDialog(FreestreamConfig())
    chk = dlg.mach_check_chk
    assert isinstance(chk, QCheckBox)
    assert "Verify Mach at each point" in chk.text()
    # directly adjacent to tunnel_ctl_chk: same Tunnel group box
    assert chk.parent() is dlg.tunnel_ctl_chk.parent()
    assert chk.parent().title() == "Tunnel"
    assert "without waiting" in chk.toolTip()      # Off = record immediately


def test_dialog_roundtrip(app):
    cfg = FreestreamConfig()
    dlg = MeasurementSetupDialog(cfg)
    assert dlg.mach_check_chk.isChecked()          # follows the default
    dlg.mach_check_chk.setChecked(False)
    dlg.apply_to(cfg)
    assert cfg.mach_check_enabled is False
    dlg2 = MeasurementSetupDialog(cfg)             # reloads the saved state
    assert not dlg2.mach_check_chk.isChecked()
    dlg2.mach_check_chk.setChecked(True)
    dlg2.apply_to(cfg)
    assert cfg.mach_check_enabled is True
