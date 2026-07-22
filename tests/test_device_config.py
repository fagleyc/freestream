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
}


def test_every_device_has_a_spec():
    assert set(EXPECTED_TABS) == set(DEVICE_SPECS)


def test_dialog_tabs_per_device(app, all_manifest):
    seen = {}
    for mode in ("mode1", "mode2"):
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


# ── per-device connect from the rail ─────────────────────────────────────
def test_single_device_connect_disconnect(app, manager):
    daq = manager.devices["daqbook"]
    assert not daq.connected
    daq.connect()
    assert daq.connected
    daq.disconnect()
    assert not daq.connected
