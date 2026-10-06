"""Named user-saved custom modes + custom-picker auto-detect.

Covers the user-request "the custom list would auto detect the device
driver, and a user could manually build and save a mode which is unique
to his custom setup":

* config.py store helpers — save/overwrite/delete/load round-trip via
  the FREESTREAM_USER_MODES override, missing/corrupt files fail soft;
* the mode combo shows saved modes (★-prefixed) between the manifest
  modes and "custom", selecting one builds its device set WITHOUT the
  picker, and the selection round-trips through FreestreamConfig;
* the picker renders auto-detected UNAVAILABLE devices as disabled rows
  (with the probe failure reason) and re-probes on "Detect devices";
* stale saved modes (device gone / driver broken) fail SOFT at select
  time and at startup. Offscreen.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication                # noqa: E402

from freestream.config import (FreestreamConfig, delete_user_mode,  # noqa: E402
                               load_user_modes, save_user_mode,
                               user_modes_path)
from freestream.manager import DEFAULT_MANIFEST, DeviceManager  # noqa: E402
from freestream.app.device_picker import DevicePickerDialog  # noqa: E402
from freestream.app.main_window import (                # noqa: E402
    USER_MODE_PREFIX, FreestreamMainWindow, build_manager)

# fakes registry with one UNAVAILABLE device: "broken" points at a class
# that does not exist, so the catalog's sim-instantiate probe fails on it
FAKES = {
    "balance": {"adapter": "freestream._fakes.FakeStreamer", "enabled": True},
    "daq": {"adapter": "freestream._fakes.FakeDaq", "enabled": True},
    "pos": {"adapter": "freestream._fakes.FakePositioner", "enabled": True},
    "tun": {"adapter": "freestream._fakes.FakeTunnel", "enabled": True},
    "broken": {"adapter": "freestream._fakes.NoSuchAdapter",
               "enabled": True},
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
def window(app, fakes_manifest, tmp_path):
    mgr = DeviceManager("mode1", sim=True, manifest_path=fakes_manifest)
    config = FreestreamConfig(config_name="umodes",
                              data_root=str(tmp_path / "runs"))
    win = FreestreamMainWindow(config, manager=mgr)
    yield win
    win.close()
    app.processEvents()
    mgr.disconnect_all()


def _items(win):
    return [win.mode_combo.itemText(i)
            for i in range(win.mode_combo.count())]


# ── config.py store helpers ──────────────────────────────────────────────
def test_store_helpers_roundtrip():
    # conftest's autouse fixture points FREESTREAM_USER_MODES at tmp
    assert load_user_modes() == {}                     # empty store
    save_user_mode("South probe rig", ["lswt_traverse", "ni_daq", "heise"])
    save_user_mode("Bare DAQ", ["ni_daq"])
    assert load_user_modes() == {
        "South probe rig": ["lswt_traverse", "ni_daq", "heise"],
        "Bare DAQ": ["ni_daq"]}
    # the file is the documented flat {name: [device ids]} mapping
    raw = json.loads(user_modes_path().read_text(encoding="utf-8"))
    assert raw["Bare DAQ"] == ["ni_daq"]
    # overwriting an existing name UPDATES it
    save_user_mode("Bare DAQ", ["ni_daq", "heise"])
    assert load_user_modes()["Bare DAQ"] == ["ni_daq", "heise"]
    # explicit delete removes exactly that one
    delete_user_mode("Bare DAQ")
    assert list(load_user_modes()) == ["South probe rig"]
    delete_user_mode("never existed")                  # no-op, no raise


def test_store_missing_corrupt_and_junk_fail_soft(tmp_path, monkeypatch):
    p = tmp_path / "modes.json"
    monkeypatch.setenv("FREESTREAM_USER_MODES", str(p))
    assert load_user_modes() == {}                     # missing file
    p.write_text("{ not json", encoding="utf-8")
    assert load_user_modes() == {}                     # corrupt file
    p.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert load_user_modes() == {}                     # wrong shape
    p.write_text(json.dumps({"ok": ["daq"], "": ["daq"], "no ids": [],
                             "wrong": "daq", "nums": [1, 2]}),
                 encoding="utf-8")
    assert load_user_modes() == {"ok": ["daq"]}        # junk entries skipped


def test_config_carries_custom_mode_name(tmp_path):
    cfg = FreestreamConfig(mode="custom", custom_devices=["pos", "daq"],
                           custom_mode_name="My rig")
    path = tmp_path / "cfg.json"
    cfg.save(path)
    loaded = FreestreamConfig.load(path)
    assert loaded.custom_mode_name == "My rig"
    assert loaded.custom_devices == ["pos", "daq"]
    # configs predating the field load with the empty default
    d = cfg.to_dict()
    d.pop("custom_mode_name")
    assert FreestreamConfig.from_dict(d).custom_mode_name == ""


# ── combo: save → appears → select → builds ──────────────────────────────
def test_saved_mode_appears_in_combo_and_builds(window):
    win = window
    assert _items(win) == ["mode1", "custom"]          # empty store
    win._save_user_mode("South survey", ["pos", "daq"])
    # manifest modes first, ★ saved modes next, "custom" always last
    assert _items(win) == ["mode1", USER_MODE_PREFIX + "South survey",
                           "custom"]
    win.mode_combo.setCurrentText(USER_MODE_PREFIX + "South survey")
    assert set(win.manager.devices) == {"pos", "daq"}  # built, no picker
    assert win.manager.mode == DeviceManager.CUSTOM
    assert win.config.mode == DeviceManager.CUSTOM
    assert win.config.custom_devices == ["pos", "daq"]
    assert win.config.custom_mode_name == "South survey"
    assert win.mode_combo.currentText() == USER_MODE_PREFIX + "South survey"
    # roles were derived from capabilities
    assert win.manager.positioner is not None
    assert win.manager.positioner.id == "pos"
    # switching back to a manifest mode clears the custom selection
    win.mode_combo.setCurrentText("mode1")
    assert win.config.custom_mode_name == ""
    assert win.config.custom_devices == []


def test_saved_mode_restores_across_restart(app, fakes_manifest, tmp_path):
    """Persistence shape: mode="custom" + custom_devices + the name —
    a fresh window shows the saved mode as current after a restart."""
    save_user_mode("My rig", ["pos", "daq"])
    cfg = FreestreamConfig(mode="custom", custom_devices=["pos", "daq"],
                           custom_mode_name="My rig", config_name="umodes",
                           data_root=str(tmp_path / "runs"))
    path = tmp_path / "cfg.json"
    cfg.save(path)
    loaded = FreestreamConfig.load(path)               # "restart"
    mgr = DeviceManager.custom(["pos", "daq"], sim=True,
                               manifest_path=fakes_manifest)
    win = FreestreamMainWindow(loaded, manager=mgr)
    try:
        assert win.mode_combo.currentText() == USER_MODE_PREFIX + "My rig"
        assert set(win.manager.devices) == {"pos", "daq"}
        assert win.config.custom_mode_name == "My rig"
    finally:
        win.close()
        app.processEvents()
        mgr.disconnect_all()


def test_delete_updates_store_and_combo(window):
    win = window
    win._save_user_mode("A", ["pos"])
    win._save_user_mode("B", ["daq"])
    assert _items(win) == ["mode1", USER_MODE_PREFIX + "A",
                           USER_MODE_PREFIX + "B", "custom"]
    # make B the ACTIVE mode, then delete it: the built set stays, the
    # name goes away and the combo shows the unnamed "custom"
    win.mode_combo.setCurrentText(USER_MODE_PREFIX + "B")
    assert set(win.manager.devices) == {"daq"}
    win._delete_user_mode("B")
    assert load_user_modes() == {"A": ["pos"]}
    assert _items(win) == ["mode1", USER_MODE_PREFIX + "A", "custom"]
    assert win.config.custom_mode_name == ""
    assert win.mode_combo.currentText() == DeviceManager.CUSTOM
    assert set(win.manager.devices) == {"daq"}         # set untouched
    # deleting a non-active mode leaves the selection alone
    win._delete_user_mode("A")
    assert load_user_modes() == {}
    assert _items(win) == ["mode1", "custom"]


# ── picker: auto-detected availability ───────────────────────────────────
def test_catalog_marks_unavailable_devices(window):
    catalog = window._device_catalog()
    assert set(catalog) == set(FAKES)
    label, caps, reason = catalog["broken"]
    assert reason                                      # probe failed
    assert "NoSuchAdapter" in reason
    label, caps, reason = catalog["daq"]
    assert reason == "" and "streaming" in caps        # probe passed
    assert "unavailable" in window.console.toPlainText()


def test_picker_disables_unavailable_rows(app, window):
    catalog = window._device_catalog()
    # preselecting an unavailable device must NOT tick it
    dlg = DevicePickerDialog(catalog, ["pos", "broken"])
    try:
        assert not dlg._boxes["broken"].isEnabled()
        assert not dlg._boxes["broken"].isChecked()
        assert "NoSuchAdapter" in dlg._boxes["broken"].toolTip()
        assert dlg._boxes["pos"].isEnabled()
        assert dlg._boxes["pos"].isChecked()
        assert dlg.selected_devices() == ["pos"]       # broken unpickable
    finally:
        dlg.deleteLater()
        app.processEvents()


def test_picker_detect_button_reprobes(app):
    cat_ok = {"a": ("A", ["streaming"], ""), "b": ("B", [], "")}
    cat_b_down = {"a": ("A", ["streaming"], ""),
                  "b": ("b", [], "ImportError: no driver")}
    state = {"catalog": cat_b_down}
    dlg = DevicePickerDialog(cat_ok, ["a", "b"],
                             refresh=lambda: state["catalog"])
    try:
        assert dlg.selected_devices() == ["a", "b"]
        dlg._detect()                                  # b went unavailable
        assert not dlg._boxes["b"].isEnabled()
        assert dlg.selected_devices() == ["a"]         # tick on a survived
        state["catalog"] = cat_ok
        dlg._detect()                                  # b came back
        assert dlg._boxes["b"].isEnabled()
        assert dlg.selected_devices() == ["a"]         # never re-ticked
    finally:
        dlg.deleteLater()
        app.processEvents()


def test_picker_save_and_delete_affordances(app):
    deleted = []
    dlg = DevicePickerDialog({"a": ("A", [], "")}, ["a"],
                             saved_modes=["Old rig", "Other"],
                             on_delete_mode=deleted.append,
                             current_name="Old rig")
    try:
        assert dlg.save_mode_name() == "Old rig"       # prefilled: re-save
        dlg._name_edit.setText("  New rig  ")
        assert dlg.save_mode_name() == "New rig"       # stripped
        dlg._saved_combo.setCurrentText("Old rig")
        dlg._delete_saved()                            # explicit delete
        assert deleted == ["Old rig"]
        assert [dlg._saved_combo.itemText(i)
                for i in range(dlg._saved_combo.count())] == ["Other"]
    finally:
        dlg.deleteLater()
        app.processEvents()


def test_picker_accept_with_name_saves_mode(window, monkeypatch):
    """Selecting "custom", ticking a set and typing a name persists the
    mode and selects its ★ item immediately."""
    win = window

    def fake_exec(dlg):
        dlg._name_edit.setText("Rig A")                # user types a name
        return 1                                       # and hits OK

    monkeypatch.setattr(DevicePickerDialog, "exec", fake_exec)
    win.mode_combo.setCurrentText("custom")            # opens the picker
    assert load_user_modes() == {
        "Rig A": ["balance", "daq", "pos", "tun"]}     # preselected set
    assert win.mode_combo.currentText() == USER_MODE_PREFIX + "Rig A"
    assert win.config.custom_mode_name == "Rig A"
    assert set(win.manager.devices) == {"balance", "daq", "pos", "tun"}
    assert win.manager.mode == DeviceManager.CUSTOM


# ── stale saved modes fail SOFT ──────────────────────────────────────────
def test_stale_saved_mode_fails_soft_on_select(window):
    win = window
    before = set(win.manager.devices)
    # a device id no longer in the manifest
    save_user_mode("ghost", ["pos", "long_gone_device"])
    win._populate_mode_combo()
    win.mode_combo.setCurrentText(USER_MODE_PREFIX + "ghost")
    assert set(win.manager.devices) == before          # unchanged, no crash
    assert win.mode_combo.currentText() == "mode1"     # reverted
    assert "failed to build" in win.console.toPlainText()
    # a device that IS in the manifest but whose driver probe fails
    save_user_mode("half broken", ["pos", "broken"])
    win._populate_mode_combo()
    win.mode_combo.setCurrentText(USER_MODE_PREFIX + "half broken")
    assert set(win.manager.devices) == before
    assert win.mode_combo.currentText() == "mode1"
    # a name missing from the store entirely
    win._select_user_mode("never saved")
    assert "missing from" in win.console.toPlainText()
    assert set(win.manager.devices) == before


def test_stale_custom_config_falls_back_at_startup():
    """build_manager with a stale persisted custom set lands on the
    DEFAULT mode of the REAL manifest — not the bundled fakes."""
    msgs = []
    mgr = build_manager("custom", True, msgs.append,
                        custom_devices=["definitely_not_a_device"])
    try:
        assert mgr.mode == "SWT-AC-Internal"
        assert mgr.manifest_path == DEFAULT_MANIFEST
        assert any("falling back to the default mode" in m for m in msgs)
    finally:
        mgr.disconnect_all()


def test_window_clears_stale_custom_config_at_startup(app, tmp_path):
    """A window built from a stale custom config follows the fallback
    manager and clears the broken selection (console says so)."""
    cfg = FreestreamConfig(mode="custom", custom_devices=["nope"],
                           custom_mode_name="Ghost", config_name="umodes",
                           data_root=str(tmp_path / "runs"))
    win = FreestreamMainWindow(cfg)                    # no injected manager
    try:
        assert win.manager.custom_devices is None      # a manifest mode
        assert win.config.mode == win.manager.mode
        assert win.config.custom_devices == []
        assert win.config.custom_mode_name == ""
        assert win.mode_combo.currentText() == win.manager.mode
        assert ("custom device selection cleared"
                in win.console.toPlainText())
    finally:
        win.close()
        app.processEvents()
        win.manager.disconnect_all()
