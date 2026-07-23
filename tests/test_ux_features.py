"""UX feature tests: detachable monitor tabs (multi-monitor), maximize
buttons on the settings dialogs, dock default widths, connect/disconnect
inside the device dialog, and the device-local "Set as Defaults" flow
into freestream's startup-defaults bundle. Offscreen."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtCore import Qt                             # noqa: E402
from PyQt6.QtWidgets import QApplication                # noqa: E402

from freestream.config import FreestreamConfig          # noqa: E402
from freestream.manager import DeviceManager            # noqa: E402
from freestream.app.device_config import DeviceConfigDialog  # noqa: E402
from freestream.app.main_window import (                # noqa: E402
    LEFT_DOCK_WIDTH, RIGHT_DOCK_WIDTH, FreestreamMainWindow)
from freestream.app.monitors import (                   # noqa: E402
    DetachedTabWindow, MonitorPanel)
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
def fakes_manifest(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"modes": MODES, "devices": FAKES}),
                        encoding="utf-8")
    return manifest


@pytest.fixture()
def panel(app, fakes_manifest):
    mgr = DeviceManager("mode1", sim=True, manifest_path=fakes_manifest)
    p = MonitorPanel(mgr, FreestreamConfig())
    yield p, mgr
    p.shutdown()
    p.deleteLater()
    app.processEvents()


@pytest.fixture(scope="module")
def sim_manager(app):
    mgr = DeviceManager("mode1", sim=True)              # real sim adapters
    yield mgr
    mgr.disconnect_all()


def _tabs(panel):
    return [panel.tabText(i) for i in range(panel.count())]


# ── detachable monitor tabs ──────────────────────────────────────────────
def test_detach_and_redock_roundtrip(panel, app):
    p, _mgr = panel
    tabs_before = _tabs(p)
    idx = tabs_before.index("Balance")
    widget = p.widget(idx)

    p.detach_tab(idx)
    app.processEvents()
    assert "Balance" not in _tabs(p)
    assert p.count() == len(tabs_before) - 1
    win = p._detached["Balance"]
    assert isinstance(win, DetachedTabWindow)
    assert win.windowTitle() == "Balance — Freestream"
    assert widget.window() is win                       # reparented
    assert not widget.isHidden()      # tab-stack hidden flag was cleared
    # a NORMAL window: system title bar with min/max/close available
    assert not (win.windowFlags() & Qt.WindowType.FramelessWindowHint)

    win.close()                                         # close → re-dock
    app.processEvents()
    assert _tabs(p) == tabs_before                      # original order
    assert p.currentIndex() == idx
    assert p._detached == {}


def test_detached_balance_tab_still_updates(panel, app):
    p, mgr = panel
    idx = _tabs(p).index("Balance")
    p.detach_tab(idx)
    app.processEvents()

    bal = mgr.by_role("balance")
    bal.connect()
    bal.start()
    p.active = True
    p._sample()                                         # manual sample tick
    p._sample()
    curve, _plot = p._bal_curves["N1"]
    xs, ys = curve.getData()
    assert xs is not None and len(xs) >= 2              # fed while detached
    p._detached["Balance"].close()
    app.processEvents()


def test_detach_survives_manager_swap(panel, app, fakes_manifest):
    p, _mgr = panel
    tabs_before = _tabs(p)
    idx = tabs_before.index("Position")
    p.detach_tab(idx)
    app.processEvents()

    mgr2 = DeviceManager("mode1", sim=True, manifest_path=fakes_manifest)
    p.set_manager(mgr2)                                 # must not raise
    assert "Position" in p._detached                    # still floating
    p._sample()                                         # loop keeps running
    p._detached["Position"].close()
    app.processEvents()
    assert _tabs(p) == tabs_before


def test_context_menu_and_doubleclick_wiring(panel):
    p, _mgr = panel
    bar = p.tabBar()
    assert bar.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu
    # double-click detaches (signal wired to detach_tab)
    idx = _tabs(p).index("Forces")
    p.tabBarDoubleClicked.emit(idx)
    assert "Forces" in p._detached
    p._detached["Forces"].close()


# ── maximize buttons on the settings dialogs ─────────────────────────────
def test_dialogs_have_maximize_buttons(app, sim_manager):
    dlg = DeviceConfigDialog(sim_manager.devices["daqbook"])
    assert dlg.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint
    dlg._pump.stop()
    dlg._stop_device_panel()

    sdlg = MeasurementSetupDialog(FreestreamConfig(), [])
    assert sdlg.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint

    from freestream.app.device_picker import DevicePickerDialog
    pdlg = DevicePickerDialog({"daqbook": ("DaqBook", ["streaming"])}, [])
    assert pdlg.windowFlags() & Qt.WindowType.WindowMaximizeButtonHint


# ── non-modal device dialogs (other windows stay interactable) ───────────
def test_device_dialog_is_non_modal_and_tracked(app, tmp_path):
    """A device dialog opened through Freestream is NON-MODAL so the main
    window + other windows stay clickable; reopening raises the same
    instance; closing removes it from the tracker (rig-fixed 2026-07-23)."""
    mgr = DeviceManager("mode1", sim=True)              # real sim adapters
    config = FreestreamConfig(config_name="uxtest",
                              data_root=str(tmp_path / "runs"))
    win = FreestreamMainWindow(config, manager=mgr)
    try:
        win._open_device_settings("daqbook")
        dlg = win._device_dialogs.get("daqbook")
        assert dlg is not None
        assert not dlg.isModal()                        # never blocks
        # reopen → same instance, no second dialog
        win._open_device_settings("daqbook")
        assert win._device_dialogs["daqbook"] is dlg
        dlg.reject()                                    # close
        app.processEvents()
        assert "daqbook" not in win._device_dialogs
    finally:
        win.close()
        app.processEvents()
        mgr.disconnect_all()


# ── dock default widths ──────────────────────────────────────────────────
def test_dock_default_widths_applied(app, fakes_manifest, tmp_path):
    mgr = DeviceManager("mode1", sim=True, manifest_path=fakes_manifest)
    config = FreestreamConfig(config_name="uxtest",
                              data_root=str(tmp_path / "runs"))
    win = FreestreamMainWindow(config, manager=mgr)
    win.show()
    for _ in range(5):
        app.processEvents()
    try:
        assert win.devices_dock.width() >= LEFT_DOCK_WIDTH - 5
        assert win.planner_dock.width() >= RIGHT_DOCK_WIDTH - 5
        assert win.devices_dock.minimumWidth() >= 260
        assert win.planner_dock.minimumWidth() >= 340
    finally:
        win.close()
        app.processEvents()


# ── connect / disconnect inside the device dialog ────────────────────────
def test_dialog_connect_button_standalone(app, sim_manager):
    daq = sim_manager.devices["daqbook"]
    assert not daq.connected
    dlg = DeviceConfigDialog(daq)
    try:
        assert dlg.conn_btn.text() == "Connect"
        assert dlg.lamp.text().startswith("OFFLINE")
        dlg.conn_btn.click()                            # direct adapter path
        assert daq.connected
        assert dlg.lamp.text().startswith("OK")
        assert dlg.conn_btn.text() == "Disconnect"
        dlg.conn_btn.click()
        assert not daq.connected
        assert dlg.lamp.text().startswith("OFFLINE")
        assert dlg.conn_btn.text() == "Connect"
    finally:
        dlg._pump.stop()
        dlg._stop_device_panel()
        if daq.connected:
            daq.disconnect()


def test_dialog_connect_uses_main_window_callbacks(app, sim_manager):
    daq = sim_manager.devices["daqbook"]
    calls = []
    dlg = DeviceConfigDialog(daq,
                             on_connect=lambda: calls.append("connect"),
                             on_disconnect=lambda: calls.append("disconnect"))
    try:
        dlg.conn_btn.click()
        assert calls == ["connect"]
        assert not daq.connected            # the CALLBACK owns the action
        daq.connect()                       # as the main window would
        dlg._refresh_state()
        assert dlg.conn_btn.text() == "Disconnect"
        dlg.conn_btn.click()
        assert calls == ["connect", "disconnect"]
    finally:
        dlg._pump.stop()
        dlg._stop_device_panel()
        if daq.connected:
            daq.disconnect()


# ── Set as Defaults flow ─────────────────────────────────────────────────
def test_set_defaults_invokes_callback_and_applies(app, sim_manager):
    daq = sim_manager.devices["daqbook"]
    hits = []
    dlg = DeviceConfigDialog(daq, on_save_defaults=lambda: hits.append(1))
    try:
        dlg.defaults_btn.click()
        assert hits == [1]
        assert dlg.applied                  # what's shown was applied first
        # daqbook's package has no defaults_path() → own-file save skipped
        assert dlg._save_device_defaults() is None
    finally:
        dlg._pump.stop()
        dlg._stop_device_panel()


def test_save_device_defaults_writes_freestream_bundle(
        app, fakes_manifest, tmp_path, monkeypatch):
    bundle = tmp_path / "defaults.json"
    monkeypatch.setenv("FREESTREAM_DEFAULTS", str(bundle))
    mgr = DeviceManager("mode1", sim=True)              # real sim adapters
    config = FreestreamConfig(config_name="uxtest",
                              data_root=str(tmp_path / "runs"))
    win = FreestreamMainWindow(config, manager=mgr)
    try:
        win._save_device_defaults("daqbook")
        assert bundle.exists()
        data = json.loads(bundle.read_text(encoding="utf-8"))
        assert "daqbook" in data["device_configs"]
    finally:
        win.close()
        app.processEvents()
        mgr.disconnect_all()


@pytest.fixture(scope="module")
def all_manifest(tmp_path_factory):
    """Manifest with traverse enabled so its adapter is constructable."""
    src = json.loads((Path(__file__).resolve().parents[1] / "freestream" /
                      "devices_manifest.json").read_text(encoding="utf-8"))
    src["devices"]["traverse"]["enabled"] = True
    src["modes"]["SWT-AC-Internal"]["traverse"] = "traverse"
    path = tmp_path_factory.mktemp("manifest") / "all.json"
    path.write_text(json.dumps(src), encoding="utf-8")
    return path


def test_traverse_own_defaults_file(app, all_manifest, tmp_path,
                                    monkeypatch):
    monkeypatch.setenv("TRAVERSE_DEFAULTS", str(tmp_path / "trav.json"))
    mgr = DeviceManager("mode1", sim=True, manifest_path=all_manifest)
    dlg = DeviceConfigDialog(mgr.devices["traverse"])
    try:
        path = dlg._save_device_defaults()
        assert path == tmp_path / "trav.json"
        assert path.exists()
        json.loads(path.read_text(encoding="utf-8"))    # valid JSON
    finally:
        dlg._pump.stop()
        dlg._stop_device_panel()


def test_lswt_own_defaults_file(app, tmp_path, monkeypatch):
    monkeypatch.setenv("LSWT_DEFAULTS", str(tmp_path))
    mgr = DeviceManager("LSWT-LSWTSting-NI", sim=True)
    dlg = DeviceConfigDialog(mgr.devices["lswt"])
    try:
        path = dlg._save_device_defaults()
        assert path is not None and path.exists()
        assert path.parent == tmp_path                  # per-tunnel file
        assert path.name.startswith("defaults_")
    finally:
        dlg._pump.stop()
        dlg._stop_device_panel()
