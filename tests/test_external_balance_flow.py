"""External balance end to end: record an ATE sim sweep, then process.

The ATE streams resolved loads, so nothing in the chain may demand a
``.vol``. This records a real sim sweep in an ATE mode, runs
``processing.process_run`` over the result, and checks that Streamlined
agrees the directory is external so its own .vol gate stays open.

Also covers the two sweep-start behaviours that ship alongside: the
configuration-folder collision prompt and the verify-off operator wait.
"""

import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "devices", _ROOT.parent / "Streamlined"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("scipy.io")

from freestream import processing                          # noqa: E402
from freestream.app.config_collision import (              # noqa: E402
    clear_config_dir, existing_runs, next_free_name)
from freestream.config import FreestreamConfig             # noqa: E402
from freestream.manager import DeviceManager               # noqa: E402
from freestream.recorder import Hdf5Recorder               # noqa: E402
from freestream.runsheet import build_grid                 # noqa: E402
from freestream.sweep import (DONE, OperatorWaitRequest,   # noqa: E402
                              PROCEED, SweepCallbacks,
                              SweepEngine)


def _wait(cond, timeout=10.0):
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _spin(app, cond, timeout=60.0):
    """Wait while pumping the Qt event loop — a GUI-driven sweep only
    advances when its signals are delivered."""
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.02)
    return False


#: the shipped mode whose balance is the external ATE; it pairs the ATE
#: with the DaqBook for tunnel conditions, which the reduction needs
ATE_MODE = "SWT-External"


def _record_ate_run(tmp_path, config_name="ext_e2e", **cfg_kw):
    mgr = DeviceManager(ATE_MODE, sim=True)
    assert mgr.roles.get("balance") == "ate", mgr.roles
    errors = mgr.connect_all()
    assert errors == {}, errors
    try:
        for s in mgr.streaming:
            s.start()
        assert _wait(lambda: mgr.record_blockers() == [], 10.0), \
            mgr.record_blockers()
        cfg = FreestreamConfig(
            mode=mgr.mode, sim=True, operator="pytest",
            config_name=config_name, data_root=str(tmp_path / "runs"),
            output_format="mat", move_timeout_s=60,
            tunnel_timeout_s=60, Sref=18.75, cref=2.86, bref=12.0,
            model_name="ATE-sim", **cfg_kw)
        cfg.ref_area, cfg.ref_chord, cfg.ref_span = 18.75, 2.86, 12.0
        rec = Hdf5Recorder(cfg.data_root, config_name=cfg.config_name,
                           output_format="mat")
        eng = SweepEngine(mgr, rec, cfg)
        points = build_grid(alpha_spec="0:2:2", dwell_s=0.1, samples=200)
        outcomes = eng.run(points)
        assert [o.status for o in outcomes] == [DONE] * len(points), \
            [f"{o.status}:{o.error}" for o in outcomes]
        return Path(rec.config_dir), cfg
    finally:
        mgr.disconnect_all()


# ── the headline: external balance processes with no .vol ───────────────
def test_process_run_external_balance_needs_no_vol(tmp_path):
    run_dir, cfg = _record_ate_run(tmp_path)
    assert not list(run_dir.glob("*.vol")), "no .vol should exist"

    msgs = []
    paths = processing.process_run(run_dir, config=cfg, facility="SWT",
                                   log=msgs.append)
    assert any("external balance" in m.lower() for m in msgs), msgs

    payload = json.loads(Path(paths["data"]).read_text(encoding="utf-8"))
    assert payload["mode"] == "external"
    assert "no .vol" in payload["cal"]["file"]
    assert len(payload["points"]) >= 2
    for p in payload["points"]:
        assert len(p["E"]) == 6
        assert all(np.isfinite(v) for v in p["E"])

    html = Path(paths["report"]).read_text(encoding="utf-8")
    assert "/*%%DATA%%*/null" not in html
    assert "extWrf" in html            # external MRC path present

    import scipy.io
    m = scipy.io.loadmat(paths["mat"], squeeze_me=True,
                         struct_as_record=False)
    case = m["case_001"]
    assert hasattr(case, "Coefficients")
    assert hasattr(case, "Balance_Channels")
    if paths["xlsx"]:
        assert Path(paths["xlsx"]).exists()


def test_streamlined_sees_the_run_as_external(tmp_path):
    """Streamlined's own probe must agree, since that is what opens its
    .vol gate in the GUI."""
    run_dir, _ = _record_ate_run(tmp_path, config_name="ext_probe")
    from utils.windtunnel.data_io import run_balance_type
    assert run_balance_type(str(run_dir)) == "external"


def test_internal_balance_still_requires_a_vol(tmp_path):
    """The relaxation must not leak: bridge volts with no calibration
    still fail loudly rather than producing fabricated coefficients."""
    mgr = DeviceManager("LSWT-N-Crescent-NI", sim=True)
    assert mgr.connect_all() == {}
    try:
        for s in mgr.streaming:
            s.start()
        assert _wait(lambda: mgr.record_blockers() == [], 10.0)
        cfg = FreestreamConfig(
            mode="LSWT-N-Crescent-NI", sim=True, config_name="int_novol",
            data_root=str(tmp_path / "runs"), output_format="mat",
            move_timeout_s=60, tunnel_timeout_s=60)
        rec = Hdf5Recorder(cfg.data_root, config_name=cfg.config_name,
                           output_format="mat")
        eng = SweepEngine(mgr, rec, cfg)
        outcomes = eng.run(build_grid(alpha_spec="0:2:2", dwell_s=0.1,
                                      samples=200))
        assert all(o.status == DONE for o in outcomes)
        run_dir = Path(rec.config_dir)
    finally:
        mgr.disconnect_all()
    for stale in run_dir.glob("*.vol"):
        stale.unlink()
    with pytest.raises(RuntimeError, match=r"\.vol is required"):
        processing.process_run(run_dir, log=lambda _m: None)


# ── configuration-folder collision ──────────────────────────────────────
def test_next_free_name_and_clear(tmp_path):
    root = tmp_path / "runs"
    (root / "F16").mkdir(parents=True)
    (root / "F16" / "run_0001_alpha_0.mat").write_bytes(b"x")
    (root / "F16" / "manifest.json").write_text("{}")
    (root / "F16" / "notes.txt").write_text("keep me")
    assert next_free_name(root, "F16") == "F16_a"

    (root / "F16_a").mkdir()
    (root / "F16_a" / "run_0001_alpha_0.mat").write_bytes(b"x")
    assert next_free_name(root, "F16") == "F16_b"

    assert len(existing_runs(root / "F16")) == 2
    assert clear_config_dir(root / "F16") == 2
    assert existing_runs(root / "F16") == []
    assert (root / "F16" / "notes.txt").exists(), "only runs are cleared"


def test_empty_folder_needs_no_prompt(tmp_path):
    root = tmp_path / "runs"
    (root / "Fresh").mkdir(parents=True)
    assert existing_runs(root / "Fresh") == []


# ── verify-off operator wait ────────────────────────────────────────────
def test_verify_off_prompts_once_per_speed_step(tmp_path):
    """With verification OFF the engine must still stop for the
    operator at each new speed, and must not re-prompt for further
    points at the same speed."""
    mgr = DeviceManager("LSWT-N-Crescent-NI", sim=True)
    assert mgr.connect_all() == {}
    seen = []
    try:
        for s in mgr.streaming:
            s.start()
        assert _wait(lambda: mgr.record_blockers() == [], 10.0)
        cfg = FreestreamConfig(
            mode="LSWT-N-Crescent-NI", sim=True, config_name="verifyoff",
            data_root=str(tmp_path / "runs"), output_format="mat",
            move_timeout_s=60, tunnel_timeout_s=60,
            tunnel_control_mode="manual", mach_check_enabled=False)

        def on_wait(req: OperatorWaitRequest) -> str:
            seen.append(req)
            return PROCEED

        rec = Hdf5Recorder(cfg.data_root, config_name=cfg.config_name,
                           output_format="mat")
        eng = SweepEngine(mgr, rec, cfg,
                          SweepCallbacks(on_operator_wait=on_wait))
        # two alphas at each of two speeds: two prompts, not four
        points = build_grid(alpha_spec="0:2:2", mach_spec="0.1,0.2",
                            dwell_s=0.1, samples=200)
        outcomes = eng.run(points)
        assert all(o.status == DONE for o in outcomes), \
            [f"{o.status}:{o.error}" for o in outcomes]
    finally:
        mgr.disconnect_all()
    assert len(seen) == 2, f"expected one prompt per speed, got {len(seen)}"
    assert all(r.verify is False for r in seen)
    assert {round(r.target_mach, 3) for r in seen} == {0.1, 0.2}


def test_verify_on_keeps_auto_proceed_flag():
    """The verify path is untouched: its requests still allow the
    dialog to auto-proceed once the measurement holds."""
    req = OperatorWaitRequest(target_mach=0.1, tolerance=0.01,
                              measure=lambda: (0.1, 1500.0))
    assert req.verify is True


def test_air_off_reprompts_when_the_speed_returns(tmp_path):
    """An air-off point turns the fan off, so returning to a speed the
    operator already confirmed must prompt again."""
    mgr = DeviceManager("LSWT-N-Crescent-NI", sim=True)
    assert mgr.connect_all() == {}
    seen = []
    try:
        for s_ in mgr.streaming:
            s_.start()
        assert _wait(lambda: mgr.record_blockers() == [], 10.0)
        cfg = FreestreamConfig(
            mode="LSWT-N-Crescent-NI", sim=True, config_name="airoff",
            data_root=str(tmp_path / "runs"), output_format="mat",
            move_timeout_s=60, tunnel_timeout_s=60,
            tunnel_control_mode="manual", mach_check_enabled=False)
        rec = Hdf5Recorder(cfg.data_root, config_name=cfg.config_name,
                           output_format="mat")
        eng = SweepEngine(
            mgr, rec, cfg,
            SweepCallbacks(on_operator_wait=lambda r: (seen.append(r),
                                                       PROCEED)[1]))
        # 0.1, then air-off, then 0.1 again: two prompts, not one
        points = build_grid(alpha_spec="0", mach_spec="0.1,0,0.1",
                            dwell_s=0.1, samples=200)
        outcomes = eng.run(points)
        assert all(o.status == DONE for o in outcomes),             [f"{o.status}:{o.error}" for o in outcomes]
    finally:
        mgr.disconnect_all()
    assert len(seen) == 2, f"air-off did not reset the gate ({len(seen)})"


# ── the collision prompt reaches the GUI start path ─────────────────────
def test_start_sweep_prompts_and_repeats(tmp_path):
    """Second sweep into a used configuration name records into the
    suffixed folder and renames the measurement configuration."""
    import json as _json
    from PyQt6.QtWidgets import QApplication
    from freestream.app.main_window import FreestreamMainWindow

    app = QApplication.instance() or QApplication([sys.argv[0]])
    fakes = {"balance": {"adapter": "freestream._fakes.FakeStreamer",
                         "enabled": True},
             "pos": {"adapter": "freestream._fakes.FakePositioner",
                     "enabled": True}}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(_json.dumps(
        {"modes": {"mode1": {"positioner": "pos", "balance": "balance"}},
         "devices": fakes}), encoding="utf-8")
    mgr = DeviceManager("mode1", sim=True, manifest_path=manifest)
    cfg = FreestreamConfig(operator="pytest", config_name="dup",
                           data_root=str(tmp_path / "runs"))
    win = FreestreamMainWindow(cfg, manager=mgr)
    asked = []
    try:
        win.connect_btn.click()
        app.processEvents()
        win.planner.alpha_edit.setText("0")
        win.planner.build_btn.click()

        win.start_btn.click()                       # empty folder: silent
        assert _spin(app, lambda: win.sweep_active, 10)
        assert _spin(app, lambda: not win.sweep_active, 60)
        assert win.config.config_name == "dup"
        assert not asked

        def resolver(parent, root, name):
            asked.append(name)
            return "repeat", next_free_name(root, name)

        win.collision_resolver = resolver
        win.start_btn.click()                       # now it must ask
        assert _spin(app, lambda: win.sweep_active, 10)
        assert _spin(app, lambda: not win.sweep_active, 60)
        assert asked == ["dup"], asked
        assert win.config.config_name == "dup_a"
        assert existing_runs(tmp_path / "runs" / "dup")
        assert existing_runs(tmp_path / "runs" / "dup_a")
    finally:
        win.close()
        app.processEvents()


def test_start_sweep_cancel_records_nothing(tmp_path):
    import json as _json
    from PyQt6.QtWidgets import QApplication
    from freestream.app.main_window import FreestreamMainWindow

    app = QApplication.instance() or QApplication([sys.argv[0]])
    fakes = {"balance": {"adapter": "freestream._fakes.FakeStreamer",
                         "enabled": True},
             "pos": {"adapter": "freestream._fakes.FakePositioner",
                     "enabled": True}}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(_json.dumps(
        {"modes": {"mode1": {"positioner": "pos", "balance": "balance"}},
         "devices": fakes}), encoding="utf-8")
    mgr = DeviceManager("mode1", sim=True, manifest_path=manifest)
    cfg = FreestreamConfig(operator="pytest", config_name="dup2",
                           data_root=str(tmp_path / "runs"))
    win = FreestreamMainWindow(cfg, manager=mgr)
    try:
        win.connect_btn.click()
        app.processEvents()
        win.planner.alpha_edit.setText("0")
        win.planner.build_btn.click()
        win.start_btn.click()
        assert _spin(app, lambda: win.sweep_active, 10)
        assert _spin(app, lambda: not win.sweep_active, 60)
        before = len(existing_runs(tmp_path / "runs" / "dup2"))

        win.collision_resolver = lambda p, r, n: ("cancel", None)
        win.start_btn.click()
        app.processEvents()
        assert not win.sweep_active                 # never launched
        assert len(existing_runs(tmp_path / "runs" / "dup2")) == before
        assert "cancelled" in win.console.toPlainText()
    finally:
        win.close()
        app.processEvents()
