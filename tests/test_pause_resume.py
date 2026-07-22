"""Sweep pause/resume — point-boundary semantics (engine + GUI).

Pause takes effect at the NEXT point boundary: the current point
finishes acquiring/writing normally, then the engine HOLDS (no motion,
no acquisition) until resume() or abort(). Abort works from paused;
E-STOP still aborts (never just pauses). GUI: the command-bar Pause
button toggles Pause⇄Resume while a sweep runs.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import json
import sys
import threading
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freestream.config import FreestreamConfig
from freestream.manager import DeviceManager
from freestream.recorder import Hdf5Recorder
from freestream.runsheet import build_grid
from freestream.sweep import DONE, SKIPPED, SweepCallbacks, SweepEngine

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


def _spin(cond, timeout_s=10.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ── engine level ─────────────────────────────────────────────────────────
def test_pause_holds_at_boundary_then_resume_finishes_all(tmp_path):
    mgr, rec, cfg = _rig(tmp_path)
    outcomes, events, paused_at = [], [], []
    engine = SweepEngine(mgr, rec, cfg, SweepCallbacks(
        on_event=events.append, on_point_done=outcomes.append,
        on_paused=lambda i, n: paused_at.append((i, n))))
    points = build_grid(alpha_spec="0:1:3", dwell_s=0.05, samples=50)
    assert len(points) == 4
    t = threading.Thread(target=engine.run, args=(points,), daemon=True)
    t.start()
    # pause as soon as the first point is under way — takes effect at
    # the NEXT boundary, so the current point completes normally
    assert _spin(lambda: engine.running)
    engine.pause()
    assert engine.pause_requested
    assert _spin(lambda: engine.paused, timeout_s=15)
    n_at_hold = len(outcomes)
    assert n_at_hold >= 1                      # current point finished…
    assert n_at_hold < len(points)             # …but the sweep didn't
    assert all(o.status == DONE for o in outcomes)
    # HOLDING: no further outcomes appear while paused
    time.sleep(1.0)
    assert len(outcomes) == n_at_hold
    assert engine.paused and engine.running
    assert any("sweep PAUSED — holding before point" in e for e in events)
    assert paused_at == [(n_at_hold, len(points))]
    # resume → the remaining points run to completion, all DONE
    engine.resume()
    t.join(timeout=30)
    assert not t.is_alive()
    assert len(outcomes) == len(points)
    assert [o.status for o in outcomes] == [DONE] * len(points)
    assert any("sweep RESUMED" in e for e in events)
    assert not engine.paused and not engine.running


def test_abort_from_paused_releases_hold_and_skips_rest(tmp_path):
    mgr, rec, cfg = _rig(tmp_path)
    outcomes = []
    engine = SweepEngine(mgr, rec, cfg, SweepCallbacks(
        on_point_done=outcomes.append))
    points = build_grid(alpha_spec="0:1:3", dwell_s=0.05, samples=50)
    results = []
    t = threading.Thread(target=lambda: results.extend(engine.run(points)),
                         daemon=True)
    t.start()
    assert _spin(lambda: engine.running)
    engine.pause()
    assert _spin(lambda: engine.paused, timeout_s=15)
    n_done = len(outcomes)
    engine.abort()
    t.join(timeout=10)
    assert not t.is_alive()
    assert not engine.running and not engine.paused
    # points completed before the hold stay DONE; the rest were skipped
    assert [r.status for r in results[:n_done]] == [DONE] * n_done
    assert all(r.status == SKIPPED for r in results[n_done:])
    assert len(results) == len(points)


def test_estop_from_paused_aborts(tmp_path):
    mgr, rec, cfg = _rig(tmp_path)
    engine = SweepEngine(mgr, rec, cfg)
    points = build_grid(alpha_spec="0:1:3", dwell_s=0.05, samples=50)
    t = threading.Thread(target=engine.run, args=(points,), daemon=True)
    t.start()
    assert _spin(lambda: engine.running)
    engine.pause()
    assert _spin(lambda: engine.paused, timeout_s=15)
    engine.estop()                       # E-STOP always aborts, never holds
    t.join(timeout=10)
    assert not t.is_alive()
    assert mgr.devices["pos"].stopped
    assert not engine.running


def test_run_clears_stale_pause_request(tmp_path):
    mgr, rec, cfg = _rig(tmp_path)
    engine = SweepEngine(mgr, rec, cfg)
    engine.pause()                       # stale request from a prior run
    points = build_grid(alpha_spec="0,1", dwell_s=0.05, samples=50)
    results = engine.run(points)         # must not hold forever
    assert [r.status for r in results] == [DONE, DONE]


# ═══ GUI: the command-bar Pause button ═══════════════════════════════════
from PyQt6.QtWidgets import QApplication               # noqa: E402

from freestream.app.main_window import FreestreamMainWindow  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


def _spin_app(app, cond, timeout_s=60.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture()
def window(app, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"modes": MODES, "devices": FAKES}),
                        encoding="utf-8")
    manager = DeviceManager("mode1", sim=True, manifest_path=manifest)
    config = FreestreamConfig(operator="pytest", config_name="pausetest",
                              data_root=str(tmp_path / "runs"),
                              samples=100, dwell_s=0.05,
                              move_timeout_s=5, tunnel_timeout_s=5)
    win = FreestreamMainWindow(config, manager=manager)
    win.show()
    app.processEvents()
    yield win
    win.close()
    app.processEvents()


def test_pause_button_idle_state(window):
    """Button exists in the command bar and is disabled while no sweep
    runs, reading 'Pause'."""
    assert window.pause_btn.text() == "Pause"
    assert not window.pause_btn.isEnabled()
    assert "Pause after current point" in window.pause_btn.toolTip()


def test_pause_button_toggles_and_sweep_pauses(app, window):
    win = window
    win.connect_btn.click()
    app.processEvents()
    win.planner.alpha_edit.setText("0:1:3")     # 4 points
    win.planner.build_btn.click()
    win.start_btn.click()
    assert _spin_app(app, lambda: win.sweep_active, timeout_s=10)
    assert win.pause_btn.isEnabled()

    win.pause_btn.click()                       # Pause → label flips
    app.processEvents()
    assert win.pause_btn.text() == "Resume"
    assert win.engine.pause_requested
    # the engine reaches the hold; status label shows the paused banner
    assert _spin_app(app, lambda: win.engine.paused, timeout_s=20)
    assert _spin_app(
        app, lambda: "SWEEP PAUSED" in win.status_lbl.text(), timeout_s=5)
    assert "holding before point" in win.status_lbl.text()
    # planner stays locked and Abort stays armed while paused
    assert win.abort_btn.isEnabled()
    assert not win.start_btn.isEnabled()

    win.pause_btn.click()                       # Resume
    app.processEvents()
    assert win.pause_btn.text() == "Pause"
    assert _spin_app(app, lambda: not win.sweep_active, timeout_s=60)
    assert all(p.status == "done" for p in win.planner.points)
    assert not win.pause_btn.isEnabled()        # idle again
    log = win.console.toPlainText()
    assert "sweep PAUSED — holding before point" in log
    assert "sweep RESUMED" in log


def test_abort_while_paused_from_gui(app, window):
    win = window
    win.connect_btn.click()
    app.processEvents()
    win.planner.alpha_edit.setText("0:1:3")
    win.planner.build_btn.click()
    win.start_btn.click()
    assert _spin_app(app, lambda: win.sweep_active, timeout_s=10)
    win.pause_btn.click()
    assert _spin_app(app, lambda: win.engine.paused, timeout_s=20)
    win.abort_btn.click()                       # abort must work from paused
    assert _spin_app(app, lambda: not win.sweep_active, timeout_s=20)
    assert any(p.status == "skipped" for p in win.planner.points)
    assert win.pause_btn.text() == "Pause"      # reset for the next run
