"""Monitor-only tunnel flow (Red Lion Block2 writes rejected).

Engine tests drive SweepCallbacks.on_operator_wait directly (headless, no
dialog); the GUI test runs the MachWaitDialog offscreen through the real
sweep worker and checks the sim auto-proceed path never writes RPM.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import json
import math
import sys
import time
from pathlib import Path

import h5py
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freestream.config import FreestreamConfig
from freestream.derived import tunnel_state
from freestream.manager import DeviceManager
from freestream.recorder import Hdf5Recorder
from freestream.runsheet import SweepPoint
from freestream.sweep import (DONE, SKIPPED, OperatorWaitRequest,
                              SweepCallbacks, SweepEngine)

FAKES = {
    "balance": {"adapter": "freestream._fakes.FakeStreamer", "enabled": True},
    "daq": {"adapter": "freestream._fakes.FakeDaq", "enabled": True},
    "pos": {"adapter": "freestream._fakes.FakePositioner", "enabled": True},
    "tun": {"adapter": "freestream._fakes.FakeTunnel", "enabled": True},
}
MODES = {"mode1": {"positioner": "pos", "balance": "balance",
                   "tunnel_conditions": "daq", "tunnel": "tun"}}

#: what FakeDaq's constant pressures read as (the ONE isentropic chain)
FAKE_DAQ_MACH = tunnel_state(0.44, 11.38, 21.0).mach


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


def _spy_set_target(mgr):
    """Record every set_target(**kw) on the tunnel; still forwards."""
    calls = []
    tun = mgr.devices["tun"]
    orig = tun.set_target

    def spy(**kw):
        calls.append(kw)
        orig(**kw)

    tun.set_target = spy
    return calls


def _mach_point(mach=0.3, **kw):
    return SweepPoint(alpha=0.0, mach=mach, dwell_s=0.05, samples=50, **kw)


# ── control disabled (the default), NO callback: headless proceed ───────
def test_monitor_only_default_no_callback_proceeds_without_writes(tmp_path):
    mgr, rec, cfg = _rig(tmp_path)
    assert cfg.tunnel_control_enabled is False        # the default
    calls = _spy_set_target(mgr)
    events = []
    engine = SweepEngine(mgr, rec, cfg,
                         SweepCallbacks(on_event=events.append))
    out = engine.run([_mach_point(0.3)])[0]
    assert out.status == DONE
    assert calls == []                                # fan NEVER commanded
    assert any("MONITOR-ONLY" in e and "proceeding immediately" in e
               for e in events)
    with h5py.File(out.path, "r") as f:
        # requested condition recorded as Mach_cmd…
        assert list(f["Tunnel/Mach_cmd"][()]) == \
            pytest.approx([0.3] * f["Tunnel/Mach_cmd"].shape[0])
        # …with the HONEST measured values alongside
        assert f["Tunnel/Mach_meas"][0] == pytest.approx(FAKE_DAQ_MACH,
                                                         abs=1e-6)
        assert f["Tunnel/RPM_meas"][0] == pytest.approx(0.0)  # untouched fan
        assert f.attrs["mach"] == 0.3


# ── callback decisions ───────────────────────────────────────────────────
def test_operator_skip_marks_point_skipped_and_continues(tmp_path):
    mgr, rec, cfg = _rig(tmp_path)
    calls = _spy_set_target(mgr)
    decisions = iter(["skip", "proceed"])
    engine = SweepEngine(mgr, rec, cfg, SweepCallbacks(
        on_operator_wait=lambda req: next(decisions)))
    results = engine.run([_mach_point(0.2), _mach_point(0.3)])
    assert [r.status for r in results] == [SKIPPED, DONE]
    assert calls == []
    assert Path(results[1].path).exists()
    assert not engine.abort_requested                 # skip ≠ abort


def test_operator_abort_aborts_whole_sweep(tmp_path):
    mgr, rec, cfg = _rig(tmp_path)
    calls = _spy_set_target(mgr)
    seen = []

    def handler(req):
        seen.append(req)
        return "abort"

    engine = SweepEngine(mgr, rec, cfg,
                         SweepCallbacks(on_operator_wait=handler))
    results = engine.run([_mach_point(0.2), _mach_point(0.25),
                          _mach_point(0.3)])
    assert [r.status for r in results] == [SKIPPED] * 3
    assert len(seen) == 1                             # asked exactly once
    assert calls == []
    assert not list((tmp_path / "runs").rglob("*.h5"))
    assert not engine.running


def test_callback_receives_working_measure_and_targets(tmp_path):
    mgr, rec, cfg = _rig(tmp_path)
    seen = {}

    def handler(req: OperatorWaitRequest):
        seen["req"] = req
        seen["measured"] = req.measure()
        return "proceed"

    engine = SweepEngine(mgr, rec, cfg,
                         SweepCallbacks(on_operator_wait=handler))
    out = engine.run([_mach_point(0.3)])[0]
    assert out.status == DONE
    req = seen["req"]
    assert req.target_mach == pytest.approx(0.3)
    assert req.target_rpm is None and not req.is_rpm
    assert req.tolerance == pytest.approx(cfg.mach_tolerance)
    mach, rpm = seen["measured"]
    # fake daq pressures → the expected isentropic Mach, live
    assert mach == pytest.approx(FAKE_DAQ_MACH, abs=1e-9)
    assert not math.isnan(mach)
    assert rpm == pytest.approx(0.0)                  # fan never commanded


# ── rpm-override points also become operator waits when disabled ─────────
def test_rpm_override_prompts_instead_of_writing_when_disabled(tmp_path):
    mgr, rec, cfg = _rig(tmp_path)
    calls = _spy_set_target(mgr)
    reqs = []

    def handler(req):
        reqs.append(req)
        return "proceed"

    events = []
    engine = SweepEngine(mgr, rec, cfg, SweepCallbacks(
        on_event=events.append, on_operator_wait=handler))
    pt = SweepPoint(alpha=0.0, dwell_s=0.05, samples=50,
                    meta={"rpm": 600.0})              # run-sheet override
    out = engine.run([pt])[0]
    assert out.status == DONE
    assert calls == []                                # STILL no writes
    req = reqs[0]
    assert req.is_rpm and req.target_mach is None
    assert req.target_rpm == pytest.approx(600.0)
    # the prompt/log speaks RPM, not Mach
    assert any("600 RPM" in e and "MONITOR-ONLY" in e for e in events)
    with h5py.File(out.path, "r") as f:
        assert f["Tunnel/RPM_cmd"][0] == pytest.approx(600.0)  # requested
        assert f["Tunnel/RPM_meas"][0] == pytest.approx(0.0)   # honest
        assert "Mach_cmd" not in f["Tunnel"]
        assert f.attrs["rpm"] == 600.0


# ── control ENABLED: the MachLoop path is untouched ──────────────────────
def test_control_enabled_still_commands_via_machloop(tmp_path):
    mgr, rec, cfg = _rig(tmp_path, tunnel_control_enabled=True)
    calls = _spy_set_target(mgr)
    waits = []
    events = []
    engine = SweepEngine(mgr, rec, cfg, SweepCallbacks(
        on_event=events.append,
        on_operator_wait=lambda req: waits.append(req) or "proceed"))
    out = engine.run([_mach_point(0.3)])[0]
    assert out.status == DONE
    assert waits == []                                # never prompted
    assert calls == [{"rpm": pytest.approx(0.3 * cfg.rpm_per_mach)}]
    assert any("sim: Mach loop proxied by RPM" in e for e in events)


# ═══ GUI: MachWaitDialog auto-proceeds in SIM through the worker ═════════
from PyQt6.QtWidgets import QApplication              # noqa: E402

from freestream.app.main_window import FreestreamMainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


def _spin(app, cond, timeout_s=60.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_gui_mach_sweep_dialog_auto_proceeds_in_sim(app, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"modes": MODES, "devices": FAKES}),
                        encoding="utf-8")
    manager = DeviceManager("mode1", sim=True, manifest_path=manifest)
    config = FreestreamConfig(operator="pytest", config_name="waittest",
                              data_root=str(tmp_path / "runs"),
                              samples=100, dwell_s=0.05,
                              move_timeout_s=5, tunnel_timeout_s=5)
    assert config.tunnel_control_enabled is False     # monitor-only default
    win = FreestreamMainWindow(config, manager=manager)
    win.show()
    app.processEvents()
    try:
        calls = _spy_set_target(win.manager)
        win.connect_btn.click()
        app.processEvents()

        # mach sweep — the standardized grammar auto-prepends air-off 0, so
        # 3 typed levels become 4 points (0, 0.2, 0.25, 0.3)
        win.planner.mach_edit.setText("0.2,0.25,0.3")
        win.planner.build_btn.click()
        app.processEvents()
        assert len(win.planner.points) == 4
        assert [p.mach for p in win.planner.points] == [0.0, 0.2, 0.25, 0.3]

        win.start_btn.click()
        # the dialog appears on the GUI thread, with the SIM note…
        assert _spin(app, lambda: win._wait_dialog is not None,
                     timeout_s=10)
        dlg = win._wait_dialog
        assert dlg.sim_lbl.isVisible()
        assert "SIM" in dlg.sim_lbl.text()
        assert "0.000" in dlg.target_lbl.text()       # first target = air-off
        # …and the whole sweep completes hands-free (1 s auto-proceed each)
        assert _spin(app, lambda: not win.sweep_active, timeout_s=60)

        assert calls == []                            # ZERO set_target calls
        h5_files = sorted((tmp_path / "runs").rglob("*.h5"))
        assert len(h5_files) == 4
        log_text = win.console.toPlainText()
        assert "decision 'proceed'" in log_text       # operator decisions
        assert log_text.count("operator wait: bring the tunnel") == 4
        assert win._wait_dialog is None               # cleaned up
    finally:
        win.close()
        app.processEvents()
