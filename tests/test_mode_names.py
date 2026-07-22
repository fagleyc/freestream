"""Intuitive mode names (Task: drop "mode N") — manifest round-trip,
legacy "mode1"/"mode2"/"mode3" aliases, config-load normalization, the
new LSWT-LSWTSting-NI mode wiring, and the manifest-driven mode combo.
"""

import json
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "devices"))

from freestream.config import FreestreamConfig
from freestream.hal import Positioner, SetpointDevice
from freestream.manager import (DEFAULT_MANIFEST, LEGACY_MODE_ALIASES,
                                DeviceManager)

NEW_MODES = ("SWT-AC-Internal", "SWT-External", "SWT-Traverse",
             "LSWT-LSWTSting-NI")


# ── manifest round-trip ──────────────────────────────────────────────────
def test_manifest_carries_the_new_names_only():
    m = json.loads(DEFAULT_MANIFEST.read_text(encoding="utf-8"))
    assert list(m["modes"]) == list(NEW_MODES)
    for legacy in LEGACY_MODE_ALIASES:
        assert legacy not in m["modes"]
    # the devices registry includes the four LSWT-era devices
    assert {"lswt_sting", "ni_daq", "heise", "lswt"} <= set(m["devices"])


def test_new_names_build_the_old_device_sets():
    mgr = DeviceManager("SWT-AC-Internal", sim=True)
    assert set(mgr.devices) == {"crescent", "strainbook", "daqbook",
                                "tunnel"}
    mgr = DeviceManager("SWT-External", sim=True)
    assert set(mgr.devices) == {"ate", "daqbook", "tunnel"}
    # the DaqBook stays the tunnel_conditions device in SWT-External
    assert mgr.roles["tunnel_conditions"] == "daqbook"
    mgr = DeviceManager("SWT-Traverse", sim=True)
    assert set(mgr.devices) == {"traverse", "daqbook"}


# ── legacy aliases ───────────────────────────────────────────────────────
def test_legacy_names_alias_to_the_new_modes():
    for legacy, current in LEGACY_MODE_ALIASES.items():
        mgr = DeviceManager(legacy, sim=True)
        assert mgr.mode == current           # normalized, incl. file meta
        want = DeviceManager(current, sim=True)
        assert set(mgr.devices) == set(want.devices)
        assert mgr.roles == want.roles


def test_alias_applies_only_when_missing_from_manifest(tmp_path):
    """A fixture manifest that defines its OWN "mode1" keeps meaning
    exactly that — no aliasing."""
    manifest = {
        "modes": {"mode1": {"positioner": "pos"}},
        "devices": {"pos": {"adapter": "freestream._fakes.FakePositioner",
                            "enabled": True}},
    }
    p = tmp_path / "m.json"
    p.write_text(json.dumps(manifest), encoding="utf-8")
    mgr = DeviceManager("mode1", sim=True, manifest_path=p)
    assert mgr.mode == "mode1"
    assert set(mgr.devices) == {"pos"}


def test_unknown_mode_still_raises():
    import pytest
    with pytest.raises(ValueError, match="unknown mode"):
        DeviceManager("mode99", sim=True)


# ── config.mode migration ────────────────────────────────────────────────
def test_config_load_normalises_legacy_mode(tmp_path):
    path = tmp_path / "cfg.json"
    FreestreamConfig(mode="mode2").save(path)
    assert FreestreamConfig.load(path).mode == "SWT-External"
    # current names round-trip untouched
    FreestreamConfig(mode="LSWT-LSWTSting-NI").save(path)
    assert FreestreamConfig.load(path).mode == "LSWT-LSWTSting-NI"
    # custom mode untouched
    FreestreamConfig(mode="custom", custom_devices=["heise"]).save(path)
    assert FreestreamConfig.load(path).mode == "custom"


# ── the new LSWT mode ────────────────────────────────────────────────────
def test_lswt_mode_builds_and_wires_roles():
    mgr = DeviceManager("LSWT-LSWTSting-NI", sim=True)
    assert set(mgr.devices) == {"lswt_sting", "ni_daq", "heise", "lswt"}
    assert mgr.roles == {"positioner": "lswt_sting", "balance": "ni_daq",
                         "tunnel_conditions": "heise", "tunnel": "lswt"}
    assert isinstance(mgr.positioner, Positioner)
    assert {a.name for a in mgr.positioner.axes()} == {"alpha", "beta"}
    assert isinstance(mgr.setpoint, SetpointDevice)
    assert {s.id for s in mgr.streaming} == {"ni_daq", "heise"}


# ── GUI: manifest-driven mode combo ──────────────────────────────────────
def test_mode_combo_lists_manifest_modes_plus_custom():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([sys.argv[0]])  # noqa: F841
    from freestream.app.main_window import FreestreamMainWindow
    mgr = DeviceManager("SWT-AC-Internal", sim=True)
    win = FreestreamMainWindow(manager=mgr)
    try:
        items = [win.mode_combo.itemText(i)
                 for i in range(win.mode_combo.count())]
        assert items == list(NEW_MODES) + [DeviceManager.CUSTOM]
        assert win.mode_combo.currentText() == "SWT-AC-Internal"
        # the custom-mode picker catalog offers EVERY manifest device
        catalog = win._device_catalog()
        assert set(catalog) == set(mgr.manifest["devices"])
    finally:
        win.close()
