"""GUI feature tests — panes toggle, device-settings routing, live Forces
overstress interlock, tunnel dashboard + results tabs. Offscreen, fakes
manifest."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication                # noqa: E402

from freestream.config import FreestreamConfig          # noqa: E402
from freestream.manager import DeviceManager            # noqa: E402
from freestream.app.main_window import FreestreamMainWindow  # noqa: E402

_CALFILES = Path(__file__).resolve().parents[2] / "Streamlined" / "CalFiles"

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
    config = FreestreamConfig(config_name="guitest",
                             data_root=str(tmp_path / "runs"))
    win = FreestreamMainWindow(config, manager=manager)
    win.show()
    app.processEvents()
    yield win
    win.close()
    app.processEvents()


def test_new_monitor_tabs_present(window):
    tabs = [window.monitors.tabText(i)
            for i in range(window.monitors.count())]
    for expected in ("Tunnel", "Forces", "Results"):
        assert expected in tabs
    # Tunnel Env merged INTO the Tunnel dashboard — no separate tab
    assert "Tunnel Env" not in tabs
    assert tabs.count("Tunnel") == 1
    assert tabs[0] == "Tunnel"


def test_panes_toggle(window, app):
    # triangle slide-out handles at the central pane's edges
    window.left_handle.setChecked(False)
    app.processEvents()
    assert window.devices_dock.isHidden()
    assert window.left_handle.text() == "▶"      # points where pane returns
    window.left_handle.setChecked(True)
    app.processEvents()
    assert not window.devices_dock.isHidden()
    assert window.left_handle.text() == "◀"

    window.right_handle.setChecked(False)
    app.processEvents()
    assert window.planner_dock.isHidden()
    assert window.right_handle.text() == "◀"
    window.right_handle.setChecked(True)


def test_config_bundle_saves_and_restores_all_devices(window, tmp_path):
    # fake adapters aren't ConfigurableAdapters, so the snapshot only holds
    # the ones exposing config_dict; the bundle must round-trip verbatim.
    window._snapshot_device_configs()
    assert set(window.config.device_configs) <= set(window.manager.devices)
    path = tmp_path / "bundle.json"
    window.config.save(path)
    reloaded = FreestreamConfig.load(path)
    assert reloaded.device_configs == window.config.device_configs


def test_device_settings_guard_no_crash(window):
    # unknown id is a no-op; fake adapters have no settings dialog → logged
    window._open_device_settings("nonexistent")
    window._open_device_settings("balance")   # fake: has_settings() False


def test_forces_overstress_blocks_recording(window):
    fp = window.monitors.forces
    assert fp.record_blocker() is None
    n_over = lambda: sum("OVERSTRESS" in b
                         for b in window.manager.record_blockers())
    assert n_over() == 0
    fp.overstress = True
    assert n_over() == 1          # overstress adds a blocker
    fp.overstress = False
    assert n_over() == 0          # and clears it


def test_sim_live_selector_rebuilds_manager(window, app):
    win = window
    assert win.sim_combo.currentText() == "SIM"
    assert win.sim_badge.text() == "SIM"
    old_mgr = win.manager
    win.sim_combo.setCurrentText("LIVE")
    app.processEvents()
    assert win.manager is not old_mgr               # rebuilt
    assert win.manager.sim is False
    assert win.config.sim is False                  # persisted on save
    assert win.sim_badge.text() == "LIVE"
    assert set(win.manager.devices) == set(old_mgr.devices)
    win.sim_combo.setCurrentText("SIM")             # and back
    app.processEvents()
    assert win.manager.sim is True
    assert win.sim_badge.text() == "SIM"


def test_sim_live_selector_locked_while_connected(window, app):
    win = window
    assert win.sim_combo.isEnabled()
    win.connect_btn.click()
    app.processEvents()
    assert not win.sim_combo.isEnabled()            # locked while connected
    mgr = win.manager
    win._on_sim_changed("LIVE")                     # belt & braces path
    assert win.manager is mgr and win.manager.sim is True
    assert win.sim_combo.currentText() == "SIM"
    win.connect_btn.click()                         # disconnect
    app.processEvents()
    assert win.sim_combo.isEnabled()


def test_unified_sample_rate_pushed_at_connect(window, app):
    win = window
    win.config.sample_rate_hz = 123.0
    win.connect_btn.click()
    app.processEvents()
    for s in win.manager.streaming:                 # every streaming device
        assert s.sample_rate() == 123.0
    win.connect_btn.click()
    app.processEvents()


def test_planner_owns_no_acquisition_or_nesting_fields(window):
    p = window.planner
    for attr in ("dwell_spin", "samples_spin", "order_combo"):
        assert not hasattr(p, attr)                 # removed UI
    window.config.dwell_s = 0.25                    # the ONE source
    window.config.samples = 42
    p.alpha_edit.setText("0,1")
    p.build_btn.click()
    assert [pt.dwell_s for pt in p.points] == [0.25, 0.25]
    assert [pt.samples for pt in p.points] == [42, 42]


def test_dock_tab_bars_on_outer_sides(window):
    from PyQt6.QtCore import Qt
    from PyQt6.QtWidgets import QTabWidget
    assert window.tabPosition(Qt.DockWidgetArea.LeftDockWidgetArea) \
        == QTabWidget.TabPosition.West
    assert window.tabPosition(Qt.DockWidgetArea.RightDockWidgetArea) \
        == QTabWidget.TabPosition.East


def test_forces_page_is_pure_readout(window):
    """The Forces page never edits the cal pointers: no file picker, no
    fit/layout combos — the StrainBook device panel is the single editor
    and this page inherits + displays."""
    fp = window.monitors.forces
    assert not hasattr(fp, "load_btn")              # no private file picker
    assert not hasattr(fp, "_browse_vol")
    assert not hasattr(fp, "bal_config")            # layout combo removed
    from PyQt6.QtWidgets import QLabel
    assert isinstance(fp.fit_lbl, QLabel)           # read-only inherit
    assert isinstance(fp.layout_lbl, QLabel)
    assert isinstance(fp.vol_lbl, QLabel)
    assert hasattr(fp, "configureBalanceRequested")  # → device dialog


def test_results_ingests_written_point(window, app):
    """A truth-named mode-2 file (ATE_Balance/Lift.. + markers) reduces
    through the resolved-load path."""
    from freestream.recorder import Hdf5Recorder
    import numpy as np
    rec = Hdf5Recorder(str(Path(window.config.data_root)), "res")
    path = rec.write_point(
        point_meta={"alpha": 3.0, "beta": 0.0, "sweep_dir": "up"},
        blocks={"ATE_Balance": {"Lift": np.ones(50), "Pitch": np.ones(50),
                                "Drag": np.ones(50), "Side": np.ones(50),
                                "Yaw": np.ones(50), "Roll": np.ones(50)},
                "Tunnel": {"q_meas": np.full(10, 0.44)}},
        rates={"ATE_Balance": 50.0, "Tunnel": 10.0},
        extra_attrs={"mode": "mode2", "balance_group": "ATE_Balance",
                     "balance_type": "external"})   # resolved-load path
    before = len(window.monitors.results._rows)
    window.monitors.point_done(path)
    assert len(window.monitors.results._rows) == before + 1
    row = window.monitors.results._rows[-1]
    assert row["alpha"] == 3.0 and row["dir"] == "up"
    assert row["Lift"] == 1.0 and row["Drag"] == 1.0


def test_results_ingests_legacy_aliased_mode2_point(window, app):
    """Old mode-2 files (StrainBook aliasing, no markers) still load
    through the legacy fallback."""
    from freestream.recorder import Hdf5Recorder
    import numpy as np
    rec = Hdf5Recorder(str(Path(window.config.data_root)), "res_legacy")
    path = rec.write_point(
        point_meta={"alpha": 2.0, "beta": 0.0, "sweep_dir": "up"},
        blocks={"StrainBook_0": {"N1": np.ones(50), "N2": np.ones(50),
                                 "Y1": np.ones(50), "Y2": np.ones(50),
                                 "Axial": np.ones(50), "Roll": np.ones(50)},
                "Tunnel": {"q_meas": np.full(10, 0.44)}},
        rates={"StrainBook_0": 100.0, "Tunnel": 10.0},
        extra_attrs={"mode": "mode2"})   # mode2 → resolved-load path
    before = len(window.monitors.results._rows)
    window.monitors.point_done(path)
    assert len(window.monitors.results._rows) == before + 1
    row = window.monitors.results._rows[-1]
    assert row["alpha"] == 2.0
    assert row["Lift"] == 1.0            # N1 → Lift via the legacy alias
