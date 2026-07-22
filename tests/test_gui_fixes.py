"""Regression tests for the hardware-in-the-loop smoke-test GUI fixes.

1. The balance .vol/fit/layout are DEVICE-owned (StrainBook panel →
   Forces tab). The Freestream Forces readout INHERITS them from the
   balance adapter each tick: clearing the device's .vol drops the cal
   and RESETS the overstress alarm so a stale blocker can no longer
   refuse acquisition; loading a new one propagates the same way.
2. Build Grid ⇄ Clear Grid toggle: once a grid exists (Build OR run-sheet
   load) the button clears the whole planner state; locked while a sweep
   runs.
3. Run-sheet import inherits the model settings + the run row's config
   name into the shared config, visible in the Measurement Setup dialog.

Offscreen, fakes manifest.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication               # noqa: E402

from freestream.config import FreestreamConfig         # noqa: E402
from freestream.manager import DeviceManager           # noqa: E402
from freestream.app.main_window import FreestreamMainWindow  # noqa: E402
from freestream.app.setup_dialog import MeasurementSetupDialog  # noqa: E402

FAKES = {
    "balance": {"adapter": "freestream._fakes.FakeStreamer", "enabled": True},
    "daq": {"adapter": "freestream._fakes.FakeDaq", "enabled": True},
    "pos": {"adapter": "freestream._fakes.FakePositioner", "enabled": True},
    "tun": {"adapter": "freestream._fakes.FakeTunnel", "enabled": True},
}
MODES = {"mode1": {"positioner": "pos", "balance": "balance",
                   "tunnel_conditions": "daq", "tunnel": "tun"}}


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


@pytest.fixture()
def window(app, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"modes": MODES, "devices": FAKES}),
                        encoding="utf-8")
    manager = DeviceManager("mode1", sim=True, manifest_path=manifest)
    config = FreestreamConfig(operator="pytest", config_name="fixtest",
                              data_root=str(tmp_path / "runs"),
                              samples=100, dwell_s=0.05,
                              move_timeout_s=5, tunnel_timeout_s=5)
    win = FreestreamMainWindow(config, manager=manager)
    win.show()
    app.processEvents()
    yield win
    win.close()
    app.processEvents()


# ── FIX 1: device clears .vol → Forces drops cal + overstress unblocks ───
def test_device_vol_clear_resets_forces_and_unblocks(window, app):
    fp = window.monitors.forces
    # a loaded cal with a latched overstress alarm (rig scenario)
    fp.cal = object()
    fp.overstress = True
    fp.alarm.setText("overstress")
    fp.alarm.setVisible(True)
    fp._loaded_vol = "C:/cal/bal.vol"
    window.config.vol_path = "C:/cal/bal.vol"
    assert any("OVERSTRESS" in b for b in window.manager.record_blockers())

    # operator clears the .vol in the StrainBook DEVICE panel (Forces tab
    # → Clear); the device config's pointer goes empty and the Freestream
    # readout inherits it on its next tick
    bal = window.manager.by_role("balance")
    bal.vol_path = ""                                # fake: instance attr
    bal.cal_type = "Linear"
    fp._sample()
    app.processEvents()

    assert fp.cal is None                            # cal dropped
    assert fp.overstress is False                    # alarm RESET
    assert fp.alarm.isHidden()
    assert "no calibration loaded" in fp.info.text()
    assert window.config.vol_path == ""              # mirror follows
    # the acquisition blocker chain is clear again
    assert not any("OVERSTRESS" in b
                   for b in window.manager.record_blockers())


def test_new_device_vol_propagates_to_forces(window, app, monkeypatch):
    """The reverse path: loading a .vol in the StrainBook device panel
    reaches the Freestream Forces readout on its next tick."""
    loaded = []
    monkeypatch.setattr(type(window.monitors.forces), "load_vol",
                        lambda self, path: loaded.append(path) or True)
    bal = window.manager.by_role("balance")
    bal.vol_path = "C:/cal/new.vol"                  # fake: instance attr
    bal.cal_type = "Cubic"
    window.monitors.forces._sample()
    assert loaded == ["C:/cal/new.vol"]
    assert window.monitors.forces._loaded_fit == "Cubic"
    assert window.monitors.forces.fit_lbl.text() == "Cubic"


def test_stale_overstress_decays_when_not_evaluating(window, app):
    """A latched overstress must never persist once the reduction can't
    re-evaluate it (tare/idle/disconnect scenarios): with no cal, the next
    tick resets the alarm instead of blocking acquisition forever."""
    fp = window.monitors.forces
    fp.cal = None
    fp.overstress = True
    fp.alarm.setVisible(True)
    fp.active = True
    fp._sample()
    assert fp.overstress is False
    assert fp.alarm.isHidden()
    assert fp.record_blocker() is None


def test_failed_vol_load_resets_overstress(window):
    fp = window.monitors.forces
    fp.overstress = True
    assert fp.load_vol("Z:/does/not/exist.vol") is False
    assert fp.overstress is False                    # no cal → no blocker
    assert fp.record_blocker() is None


# ── load bars: rolling peak-hold, reset on tare ──────────────────────────
def test_load_bar_peak_hold_and_tare_reset(window):
    from freestream.app.forces import PEAK_HOLD_S, LoadBar
    fp = window.monitors.forces
    t0 = 100.0
    assert fp._rolling_peak("N1", 0.5, t0) == 0.5
    assert fp._rolling_peak("N1", 0.2, t0 + 1) == 0.5      # peak held
    # peaks age out of the rolling window…
    assert fp._rolling_peak("N1", 0.1, t0 + PEAK_HOLD_S + 2) == 0.1
    # …and tare (zero_count change) resets immediately
    fp._rolling_peak("N1", 0.9, t0 + PEAK_HOLD_S + 3)
    fp._reset_peaks()
    assert fp._rolling_peak("N1", 0.05, t0 + PEAK_HOLD_S + 4) == 0.05

    # the zero_count hook drives the reset from the balance adapter
    bal = window.manager.by_role("balance")
    fp._rolling_peak("N1", 0.8, t0 + PEAK_HOLD_S + 5)
    bal.zero_count = 7                                     # fake: attr
    fp._sample()
    assert fp._peak_hist == {}                             # peaks cleared

    # the bars are the custom peak-marker widgets (not QProgressBar)
    assert all(isinstance(b, LoadBar) for b in fp.util_bars.values())


def test_balance_tab_routes_excitation_to_strip(window):
    """The Balance monitor plots bridges on the main plot and Excitation
    on the slim linked strip below (10 V would flatten the µV bridges)."""
    m = window.monitors
    assert m._bal_exc_plot is not m._bal_plot
    curve, plot = m._bal_curves["Excitation"]
    assert plot is m._bal_exc_plot
    curve, plot = m._bal_curves["N1"]
    assert plot is m._bal_plot
    # an excitation channel exists → the strip is shown
    assert not m._bal_exc_plot.isHidden()


def test_balance_tab_hides_empty_excitation_strip(window, app):
    """A balance without an excitation channel (the ATE's resolved
    Lift/Drag/… set) leaves the exc strip HIDDEN, not an empty plot; the
    curves rebuild from channels() so the true names appear."""
    m = window.monitors
    bal = window.manager.devices["balance"]
    bal._channels = ("Lift", "Pitch", "Drag", "Side", "Yaw", "Roll")
    m._discover()
    assert set(m._bal_curves) == {"Lift", "Pitch", "Drag", "Side",
                                  "Yaw", "Roll"}
    assert all(plot is m._bal_plot
               for _c, plot in m._bal_curves.values())
    assert m._bal_exc_plot.isHidden()


def test_balance_tab_excludes_position_channels(app):
    """The ATE streams Alpha/Beta as position-kind channels — they belong
    on the Position tab, never on the balance plot."""
    from freestream.manager import DeviceManager
    from freestream.app.monitors import MonitorPanel
    m = MonitorPanel(DeviceManager("mode2", sim=True), FreestreamConfig())
    try:
        assert "Alpha" not in m._bal_curves and "Beta" not in m._bal_curves
        assert "Lift" in m._bal_curves
    finally:
        m.shutdown()


def test_saved_config_bundle_never_flips_sim_live(app):
    """Rig regression 2026-07-22: a device-config bundle snapshotted in
    SIM carried force_sim=True; applying it onto a LIVE session's
    adapter silently swapped the real StrainBook for the emulator while
    every badge said LIVE ('streams wrong results, excitation frozen').
    The manager's SIM/LIVE selection must survive apply_config_dict in
    BOTH directions, and the status badge must report the driver truth."""
    from freestream.manager import DeviceManager
    mgr = DeviceManager("mode1", sim=True)
    bal = mgr.by_role("balance")
    assert bal.config.force_sim is True
    # a bundle saved during a LIVE session must not un-sim a SIM session
    live_bundle = dict(bal.config_dict(), force_sim=False)
    bal.apply_config_dict(live_bundle)
    assert bal.config.force_sim is True
    # ...and the reverse: a SIM-era bundle must not flip a LIVE adapter
    mgr_live = DeviceManager("mode1", sim=False)
    bal_live = mgr_live.by_role("balance")
    assert bal_live.config.force_sim is False
    sim_bundle = dict(bal_live.config_dict(), force_sim=True)
    bal_live.apply_config_dict(sim_bundle)
    assert bal_live.config.force_sim is False
    assert bal_live.status().sim is False
    # honesty backstop: if force_sim ever diverges anyway, the badge says SIM
    bal_live.config.force_sim = True
    assert bal_live.status().sim is True


# ── defaults store (separate from Save/Load Config files) ────────────────
def test_set_as_defaults_stores_everything(window, tmp_path, monkeypatch):
    monkeypatch.setenv("FREESTREAM_DEFAULTS", str(tmp_path / "defaults.json"))
    from freestream.config import defaults_path
    window.config.sample_rate_hz = 456.0
    window.config.data_root = str(tmp_path / "mydata")
    window._save_defaults()
    assert defaults_path().exists()
    back = FreestreamConfig.load(defaults_path())
    assert back.sample_rate_hz == 456.0
    assert back.data_root == str(tmp_path / "mydata")
    assert isinstance(back.device_configs, dict)


def test_setup_dialog_ok_plus_defaults_button(window, app, tmp_path,
                                              monkeypatch):
    monkeypatch.setenv("FREESTREAM_DEFAULTS", str(tmp_path / "defaults.json"))
    from freestream.config import defaults_path

    def fake_exec(dlg):
        assert dlg.defaults_btn.text() == "OK + Set as Defaults"
        dlg.defaults_requested = True                  # button clicked
        return 1
    monkeypatch.setattr(MeasurementSetupDialog, "exec", fake_exec)
    window._open_setup()
    app.processEvents()
    assert defaults_path().exists()


# ── FIX 2: Build Grid ⇄ Clear Grid toggle ────────────────────────────────
def test_build_grid_toggles_to_clear_grid(window, app):
    p = window.planner
    assert p.build_btn.text() == "Build Grid"
    p.alpha_edit.setText("0,1,2")
    p.build_btn.click()
    app.processEvents()
    assert len(p.points) == 3
    assert p.build_btn.text() == "Clear Grid"        # toggled

    p.build_btn.click()                              # now CLEARS
    app.processEvents()
    assert p.points == []
    assert p.table.rowCount() == 0
    assert p.alpha_edit.text() == ""                 # axis specs blanked
    assert "no run sheet loaded" in p.indicator.text()
    assert "0 points" in p.summary_lbl.text()
    assert p.progress.value() == 0
    assert p.build_btn.text() == "Build Grid"        # re-toggled

    # building again works after a clear
    p.alpha_edit.setText("0,1")
    p.build_btn.click()
    assert len(p.points) == 2
    assert p.build_btn.text() == "Clear Grid"


def test_clear_grid_locked_while_sweep_runs(window):
    p = window.planner
    p.alpha_edit.setText("0,1")
    p.build_btn.click()
    assert p.build_btn.isEnabled()
    p.set_sweep_running(True)                        # main-window hook
    assert not p.build_btn.isEnabled()               # can't clear a live plan
    n = len(p.points)
    p.clear_grid()                                   # programmatic path too
    assert len(p.points) == n                        # refused
    p.set_sweep_running(False)
    assert p.build_btn.isEnabled()
    p.build_btn.click()
    assert p.points == []


def test_runsheet_load_sets_clear_grid_and_clears_run_state(window, app):
    pytest.importorskip("openpyxl")
    from freestream.runbook import build_run_points, load_runbook
    from test_runbook import (DEFAULT_CONFIGS, DEFAULT_NAMED, DEFAULT_RUNS,
                              make_workbook)
    tmp = Path(window.config.data_root).parent
    book = load_runbook(make_workbook(
        tmp / "rs_toggle.xlsx", runs=DEFAULT_RUNS, configs=DEFAULT_CONFIGS,
        named=DEFAULT_NAMED, ref={"Sref": 2.5, "cref": 0.5, "bref": 5.0},
        info={"test_name": "T-1", "model_name": "NACA0012"}))
    p = window.planner
    run = book.runs[0]
    p.apply_run_selection(book, run, build_run_points(book, run))
    app.processEvents()
    assert p.build_btn.text() == "Clear Grid"        # run-sheet load toggles
    assert "run_a" in p.indicator.text()

    p.build_btn.click()                              # clear after a load
    app.processEvents()
    assert p.points == []
    assert p._runbook is None and p._run_row is None  # applied-run state gone
    assert "no run sheet loaded" in p.indicator.text()
    assert p.mach_edit.text() == ""
    # …but the measurement settings it inherited stay in the CONFIG
    assert window.config.model_name == "NACA0012"
    assert window.config.Sref == 2.5


# ── FIX 3: run-sheet import inherits model settings + config name ────────
def test_run_selection_inherits_config_name_and_model_settings(window, app):
    pytest.importorskip("openpyxl")
    from freestream.runbook import build_run_points, load_runbook
    from test_runbook import (DEFAULT_CONFIGS, DEFAULT_NAMED, DEFAULT_RUNS,
                              make_workbook)
    tmp = Path(window.config.data_root).parent
    book = load_runbook(make_workbook(
        tmp / "rs_inherit.xlsx", runs=DEFAULT_RUNS, configs=DEFAULT_CONFIGS,
        named=DEFAULT_NAMED,
        ref={"Sref": 2.5, "cref": 0.5, "bref": 5.0,
             "MRC_x": 1.0, "MRC_y": 0.0, "MRC_z": 0.25},
        info={"test_name": "T-1", "model_name": "NACA0012",
              "engineer": "Casey", "operator": "cadet",
              "data_prefix": "n12"}))
    cfg = window.config
    assert cfg.config_name == "fixtest"
    run = book.runs[0]                               # run_a, config "clean"
    window.planner.apply_run_selection(book, run,
                                       build_run_points(book, run))
    app.processEvents()

    # the run row's Model Config NAME → config_name (output folder)
    assert cfg.config_name == "clean"
    # model settings landed in the config
    assert cfg.test_name == "T-1"
    assert cfg.model_name == "NACA0012"
    assert cfg.engineer == "Casey"
    assert cfg.data_prefix == "n12"
    assert cfg.operator == "cadet"                   # inherited (logged)
    assert cfg.Sref == 2.5 and cfg.cref == 0.5 and cfg.bref == 5.0
    log = window.console.toPlainText()
    assert "operator → 'cadet'" in log               # clobber is logged
    assert "config name → 'clean'" in log
    # runApplied rebuilt the recorder with the new configuration name
    assert window.recorder.config_name == "clean"

    # …and they are VISIBLE next time Measurement Setup opens
    dlg = MeasurementSetupDialog(cfg)
    assert dlg.config_name_edit.text() == "clean"
    assert dlg.operator_edit.text() == "cadet"
    assert dlg.test_name_edit.text() == "T-1"
    assert dlg.model_name_edit.text() == "NACA0012"
    assert dlg.engineer_edit.text() == "Casey"
    assert dlg.prefix_edit.text() == "n12"
    assert "Sref 2.5" in dlg.ref_dims_lbl.text()
    assert "bref 5" in dlg.ref_dims_lbl.text()
    dlg.deleteLater()


def test_setup_dialog_model_group_roundtrip(app):
    cfg = FreestreamConfig()
    dlg = MeasurementSetupDialog(cfg)
    assert "not set" in dlg.ref_dims_lbl.text()      # no run sheet yet
    dlg.test_name_edit.setText("T-9")
    dlg.model_name_edit.setText("X-29")
    dlg.engineer_edit.setText("Casey")
    dlg.prefix_edit.setText("x29")
    dlg.apply_to(cfg)
    assert cfg.test_name == "T-9"
    assert cfg.model_name == "X-29"
    assert cfg.engineer == "Casey"
    assert cfg.data_prefix == "x29"
    dlg.deleteLater()
