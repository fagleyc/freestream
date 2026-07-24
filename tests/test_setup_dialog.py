"""Measurement Setup dialog + output-format wiring tests.

Covers the GUI-polish round:

* the dialog no longer shows the per-point tare checkbox or the
  per-device cal-pointer table (config fields + engine capability stay);
* the Output section exposes the file-format pulldown (h5 / mat / xlsx)
  bound to ``config.output_format``;
* the ONE Balance .vol pointer row survives (the Forces panel reads it);
* the main window builds its recorder with ``output_format`` from the
  config and a settings change takes effect for the NEXT sweep (no
  restart).

Offscreen, fakes manifest.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication              # noqa: E402

from freestream.config import FreestreamConfig        # noqa: E402
from freestream.manager import DeviceManager          # noqa: E402
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
    config = FreestreamConfig(operator="pytest", config_name="setuptest",
                              data_root=str(tmp_path / "runs"),
                              samples=100, dwell_s=0.05,
                              move_timeout_s=5, tunnel_timeout_s=5)
    win = FreestreamMainWindow(config, manager=manager)
    win.show()
    app.processEvents()
    yield win
    win.close()
    app.processEvents()


def _spin(app, cond, timeout_s=60.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


# ── dialog surface ───────────────────────────────────────────────────────
def test_dialog_dropped_tare_and_cal_table(app):
    dlg = MeasurementSetupDialog(FreestreamConfig(), ["a", "b"])
    assert not hasattr(dlg, "zero_chk")           # tare toggle removed
    assert not hasattr(dlg, "cal_table")          # per-device table removed
    # …but the config field + engine capability remain for JSON/API use
    assert FreestreamConfig().zero_each_point is False
    assert "cal_files" in FreestreamConfig.__dataclass_fields__


def test_dialog_vol_row_is_device_owned_readout(app):
    """The .vol pointer is DEVICE-owned (StrainBook panel → Forces tab):
    Measurement Setup only displays it and apply_to leaves it alone."""
    cfg = FreestreamConfig(vol_path="C:/cal/bal.vol")
    dlg = MeasurementSetupDialog(cfg)
    assert not hasattr(dlg, "vol_edit")           # editor removed
    assert "bal.vol" in dlg.vol_note.text()
    assert "StrainBook device panel" in dlg.vol_note.text()
    out = FreestreamConfig(vol_path="C:/cal/bal.vol")
    dlg.apply_to(out)
    assert out.vol_path == "C:/cal/bal.vol"       # untouched by the dialog


def test_dialog_output_section_binds_output_format(app):
    cfg = FreestreamConfig()
    assert cfg.output_format == "h5"              # default stays HDF5
    dlg = MeasurementSetupDialog(cfg)
    # pulldown offers exactly h5 / mat / xlsx, defaulting to the config
    values = [dlg.format_combo.itemData(i)
              for i in range(dlg.format_combo.count())]
    assert values == ["h5", "mat", "xlsx"]
    assert dlg.format_combo.currentData() == "h5"
    dlg.format_combo.setCurrentIndex(dlg.format_combo.findData("mat"))
    dlg.apply_to(cfg)
    assert cfg.output_format == "mat"
    # and it round-trips back into a fresh dialog (xlsx too)
    dlg2 = MeasurementSetupDialog(cfg)
    assert dlg2.format_combo.currentData() == "mat"
    cfg.output_format = "xlsx"
    dlg3 = MeasurementSetupDialog(cfg)
    assert dlg3.format_combo.currentData() == "xlsx"
    # the old checkbox is gone
    assert not hasattr(dlg, "mat_chk")


def test_dialog_apply_roundtrip(app):
    cfg = FreestreamConfig()
    dlg = MeasurementSetupDialog(cfg)
    dlg.operator_edit.setText("casey")
    dlg.rate_spin.setValue(500.0)
    dlg.samples_spin.setValue(1234)
    dlg.mach_settle_spin.setValue(3.5)
    dlg.mach_tol_spin.setValue(0.02)
    dlg.control_mode_combo.setCurrentIndex(
        dlg.control_mode_combo.findData("regulate"))
    dlg.apply_to(cfg)
    assert cfg.operator == "casey"
    assert cfg.sample_rate_hz == 500.0
    assert cfg.samples == 1234
    assert cfg.mach_settle_s == 3.5
    assert cfg.mach_tolerance == 0.02
    assert cfg.tunnel_control_mode == "regulate"
    assert cfg.tunnel_control_enabled is True          # legacy kept in sync
    # untouched fields keep their defaults (nothing writes zero/cal)
    assert cfg.zero_each_point is False
    assert cfg.cal_files == {}


def test_output_format_survives_config_json(tmp_path):
    cfg = FreestreamConfig(output_format="xlsx")
    path = tmp_path / "cfg.json"
    cfg.save(path)
    assert FreestreamConfig.load(path).output_format == "xlsx"


def test_old_write_mat_config_loads_gracefully(tmp_path):
    """Pre-pulldown configs carried ``write_mat`` — from_dict drops the
    unknown key and the format falls back to the h5 default."""
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"operator": "casey", "write_mat": True}),
                    encoding="utf-8")
    cfg = FreestreamConfig.load(path)
    assert cfg.operator == "casey"
    assert cfg.output_format == "h5"
    assert "write_mat" not in FreestreamConfig.__dataclass_fields__


# ── recorder wiring ──────────────────────────────────────────────────────
def test_recorder_built_with_output_format_from_config(window):
    assert window.recorder.output_format == "h5"  # default
    window.config.output_format = "mat"
    rec = window._make_recorder()
    assert rec.output_format == "mat"


def test_format_change_takes_effect_for_next_sweep(app, window, tmp_path):
    win = window
    win.connect_btn.click()
    app.processEvents()

    # sweep 1 — default format: .h5 only
    win.planner.alpha_edit.setText("0")
    win.planner.build_btn.click()
    win.start_btn.click()
    assert _spin(app, lambda: win.sweep_active, timeout_s=10)
    assert _spin(app, lambda: not win.sweep_active, timeout_s=60)
    runs = tmp_path / "runs"
    assert len(list(runs.rglob("*.h5"))) == 1
    assert not list(runs.rglob("*.mat"))

    # settings change (what Measurement Setup's OK does) — NO restart
    win.config.output_format = "mat"

    # sweep 2 — _launch rebuilds the recorder from the live config
    win.start_btn.click()
    assert _spin(app, lambda: win.sweep_active, timeout_s=10)
    assert _spin(app, lambda: not win.sweep_active, timeout_s=60)
    mats = list(runs.rglob("*.mat"))
    assert len(mats) == 1                          # the point IS the .mat
    assert len(list(runs.rglob("*.h5"))) == 1      # still just sweep 1's
    assert mats[0].name.startswith("run_0002")     # numbering continues
    # run log shows the real primary path ("point N done → …")
    assert str(mats[0]) in win.console.toPlainText()
