"""Tests for the full per-device configuration system: reflection
ConfigForm coverage, DeviceConfigDialog tab assembly (channels / axis /
calibration), apply/cancel semantics, and per-device connect."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication                # noqa: E402

from freestream.manager import DeviceManager            # noqa: E402
from freestream.app.config_form import ConfigForm       # noqa: E402
from freestream.app.device_config import (              # noqa: E402
    DEVICE_SPECS, DeviceConfigDialog)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


@pytest.fixture(scope="module")
def manager(app):
    mgr = DeviceManager("mode1", sim=True)
    yield mgr
    mgr.disconnect_all()


@pytest.fixture(scope="module")
def all_manifest(tmp_path_factory):
    """Manifest with traverse enabled so every adapter is constructable."""
    src = json.loads((Path(__file__).resolve().parents[1] / "freestream" /
                      "devices_manifest.json").read_text(encoding="utf-8"))
    src["devices"]["traverse"]["enabled"] = True
    src["modes"]["SWT-AC-Internal"]["traverse"] = "traverse"
    path = tmp_path_factory.mktemp("manifest") / "all.json"
    path.write_text(json.dumps(src), encoding="utf-8")
    return path


# ── ConfigForm ────────────────────────────────────────────────────────────
def test_config_form_covers_every_scalar_field(app, manager):
    daq = manager.devices["daqbook"]
    form = ConfigForm(daq.config)
    import dataclasses
    scalars = {f.name for f in dataclasses.fields(daq.config)
               if isinstance(getattr(daq.config, f.name),
                             (bool, int, float, str))
               and f.name != "force_sim"}
    assert set(form.fields()) == scalars   # nothing hidden, nothing extra


def test_config_form_apply_roundtrip(app, manager):
    daq = manager.devices["daqbook"]
    form = ConfigForm(daq.config)
    _w, _get, set_ = form._editors["scan_hz"]
    set_(432.0)
    _w2, _g2, set_ip = form._editors["device_ip"]
    set_ip("10.0.0.9")
    form.apply()
    assert daq.config.scan_hz == 432.0
    assert daq.config.device_ip == "10.0.0.9"
    assert isinstance(daq.config.poll_ms, int)          # types preserved


def test_config_form_choices_render_combo(app, manager):
    tun = manager.devices["tunnel"]
    form = ConfigForm(tun.config,
                      choices={"word_order": ("low_first", "high_first")})
    widget, get, set_ = form._editors["word_order"]
    from PyQt6.QtWidgets import QComboBox
    assert isinstance(widget, QComboBox)
    set_("high_first")
    form.apply()
    assert tun.config.word_order == "high_first"
    set_("low_first")
    form.apply()


# ── DeviceConfigDialog assembly ───────────────────────────────────────────
EXPECTED_TABS = {
    # EVERY device embeds its ENTIRE standalone device panel as the primary
    # tab (&& = literal & — Qt mnemonic escape). Channels / Calibration /
    # Diagnostics tabs live INSIDE those panels now, exactly as in the
    # standalone apps — never duplicated as separate dialog tabs.
    "crescent": ["Motion && Calibration", "Settings", "Alpha axis",
                 "Beta axis"],
    "strainbook": ["Live && Channels", "Settings"],
    "daqbook": ["Live && Channels", "Settings"],
    "tunnel": ["Monitor && Control", "Settings"],
    "traverse": ["Motion && Calibration", "Settings", "X axis", "Y axis",
                 "Z axis"],
    "ate": ["Live && Motion && Run", "Settings"],
    "lswt": ["Monitor && Control", "Settings"],
    "lswt_sting": ["Motion && Limits", "Settings", "Alpha axis",
                   "Beta axis"],
    "ni_daq": ["Live && Channels", "Settings"],
    "heise": ["Live && History", "Settings"],
}


def test_every_device_has_a_spec():
    assert set(EXPECTED_TABS) == set(DEVICE_SPECS)


def test_dialog_tabs_per_device(app, all_manifest):
    seen = {}
    for mode in ("mode1", "mode2", "LSWT-LSWTSting-NI"):
        mgr = DeviceManager(mode, sim=True, manifest_path=all_manifest)
        for dev_id, dev in mgr.devices.items():
            if dev_id in seen:
                continue
            dlg = DeviceConfigDialog(dev)
            seen[dev_id] = [dlg.tabs.tabText(i)
                            for i in range(dlg.tabs.count())]
            dlg._pump.stop()
            dlg._stop_device_panel()
    assert seen == EXPECTED_TABS


def test_dialog_apply_and_cancel_semantics(app, manager):
    # scan_hz is suite-owned (global sample rate) and hidden from the
    # dialog, so apply/cancel semantics are exercised on buffer_seconds
    daq = manager.devices["daqbook"]
    baseline = daq.config.buffer_seconds

    dlg = DeviceConfigDialog(daq)
    _w, _get, set_ = dlg._forms[0]._editors["buffer_seconds"]
    set_(baseline + 100)
    dlg._apply()
    assert daq.config.buffer_seconds == baseline + 100
    dlg._pump.stop()

    # cancel restores the snapshot taken at open — including live edits
    # made by the channels table directly on the config
    dlg2 = DeviceConfigDialog(daq)
    daq.config.buffer_seconds = 9999.0                 # simulate table edit
    first = daq.config.channels[0].enabled
    daq.config.channels[0].enabled = not first
    dlg2.reject()
    assert daq.config.buffer_seconds == baseline + 100
    assert daq.config.channels[0].enabled == first

    daq.config.buffer_seconds = baseline               # restore for others


def test_dialog_hides_suite_owned_fields(app, manager):
    """The global sample rate + shared .vol pointers must have exactly ONE
    editor (Measurement Setup / Forces) — never the per-device dialog."""
    hidden = {"daqbook": ("scan_hz",),
              "strainbook": ("scan_hz", "vol_path", "cal_type",
                             "balance_config", "warn_utilization")}
    for dev_id, names in hidden.items():
        dlg = DeviceConfigDialog(manager.devices[dev_id])
        fields = set()
        for form in dlg._forms:
            fields |= set(form.fields())
        for name in names:
            assert name not in fields, f"{dev_id}: {name} still editable"
        dlg._pump.stop()


def test_calibration_pump_with_connected_drive(app, manager):
    cres = manager.devices["crescent"]
    cres.connect()
    try:
        dlg = DeviceConfigDialog(cres)
        # the crescent's cal UI lives inside the embedded device panel now
        assert dlg._device_panel is not None
        assert dlg._device_panel.cal_panel is not None
        dlg._pump_cal()                                # must not raise
        dlg._device_panel._refresh_ui()                # ditto (live pump)
        dlg._pump.stop()
        dlg._stop_device_panel()
    finally:
        cres.disconnect()


def test_driver_property_exposed(app, manager):
    for dev in manager.devices.values():
        assert dev.driver is not None


# ── lswt_sting axis tabs: the indexer motion limits (Feature 2) ──────────
def _axis_form(dlg, field):
    """The ConfigForm in dlg carrying `field` (skips the main Settings
    form / device panel)."""
    for form in dlg._forms:
        if field in form.fields():
            return form
    raise AssertionError(f"no form exposes {field!r}")


def test_lswt_sting_has_axis_tabs_with_motion_limits(app):
    """The sting dialog now grows Alpha/Beta axis tabs whose ONLY editors
    are the indexer velocity/accel/decel motion limits (rendered as line
    edits — the values are indexer command strings)."""
    from PyQt6.QtWidgets import QLineEdit
    mgr = DeviceManager("LSWT-LSWTSting-NI", sim=True)
    sting = mgr.devices["lswt_sting"]
    dlg = DeviceConfigDialog(sting)
    try:
        titles = [dlg.tabs.tabText(i) for i in range(dlg.tabs.count())]
        assert "Alpha axis" in titles and "Beta axis" in titles
        alpha_form = _axis_form(dlg, "velocity")
        # exactly the three motion tokens — NO travel-limit/cal duplicates
        assert set(alpha_form.fields()) == {
            "velocity", "acceleration", "deceleration"}
        # indexer strings → line edits (no float coercion of the tokens)
        w, _g, _s = alpha_form._editors["velocity"]
        assert isinstance(w, QLineEdit)
    finally:
        dlg._pump.stop()
        dlg._stop_device_panel()


def test_lswt_sting_axis_edit_applies_to_driver_config(app):
    """Editing an axis's velocity/acceleration and Apply writes the exact
    string tokens back to the adapter's driver config (and rebinds)."""
    mgr = DeviceManager("LSWT-LSWTSting-NI", sim=True)
    sting = mgr.devices["lswt_sting"]
    dlg = DeviceConfigDialog(sting)
    try:
        # find the Alpha vs Beta forms by their live object identity
        forms = {id(f._obj): f for f in dlg._forms}
        alpha_form = forms[id(sting.config.alpha)]
        beta_form = forms[id(sting.config.beta)]
        alpha_form._editors["velocity"][2](".200")     # setter
        alpha_form._editors["acceleration"][2]("12.5")
        beta_form._editors["deceleration"][2]("3.3")
        dlg._apply()
        # round-trips to the DRIVER's config object (driver.config is the
        # same live StingConfig the adapter holds)
        assert sting.driver.config.alpha.velocity == ".200"
        assert sting.driver.config.alpha.acceleration == "12.5"
        assert sting.driver.config.beta.deceleration == "3.3"
        # still strings — the indexer wants the exact tokens
        assert isinstance(sting.driver.config.alpha.velocity, str)
    finally:
        dlg._pump.stop()
        dlg._stop_device_panel()


def test_modbus_axis_sections_unchanged_regression(app, all_manifest):
    """crescent/traverse axis tabs keep rendering the default Modbus-shaped
    AXIS_SECTIONS (per-spec override is opt-in; they fall back to it)."""
    from freestream.app.device_config import AXIS_SECTIONS
    for dev_id in ("crescent", "traverse"):
        spec = DEVICE_SPECS[dev_id]
        assert spec["axis_sections"] is AXIS_SECTIONS   # default, untouched

    mgr = DeviceManager("mode1", sim=True, manifest_path=all_manifest)
    cres = mgr.devices["crescent"]
    dlg = DeviceConfigDialog(cres)
    try:
        alpha = _axis_form(dlg, "ip")                   # a Modbus axis field
        # the crescent's Communication section (Modbus ip/port) still shows
        assert {"ip", "port", "unit_id"} <= set(alpha.fields())
    finally:
        dlg._pump.stop()
        dlg._stop_device_panel()


def test_lswt_sting_dialog_motion_tabs_screenshot(app, tmp_path):
    """Render the sting dialog offscreen, confirm the Motion tabs, and save
    a screenshot to disk (path returned by the harness)."""
    mgr = DeviceManager("LSWT-LSWTSting-NI", sim=True)
    sting = mgr.devices["lswt_sting"]
    dlg = DeviceConfigDialog(sting)
    try:
        dlg.resize(900, 640)
        # select the Alpha axis tab so the Motion limits show in the grab
        idx = next(i for i in range(dlg.tabs.count())
                   if dlg.tabs.tabText(i) == "Alpha axis")
        dlg.tabs.setCurrentIndex(idx)
        app.processEvents()
        out = Path(tmp_path) / "lswt_sting_motion_tab.png"
        assert dlg.grab().save(str(out))
        assert out.exists() and out.stat().st_size > 0
    finally:
        dlg._pump.stop()
        dlg._stop_device_panel()


# ── per-device connect from the rail ─────────────────────────────────────
def test_single_device_connect_disconnect(app, manager):
    daq = manager.devices["daqbook"]
    assert not daq.connected
    daq.connect()
    assert daq.connected
    daq.disconnect()
    assert not daq.connected
