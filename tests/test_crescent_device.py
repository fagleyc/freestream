"""Crescent adapter + device-dialog regression tests.

Covers the reported bugs and the streamlined dialog:

* a driver config loaded/edited through the adapter must be REBOUND to the
  running CrescentDrive (``set_config``) -- previously the drive silently
  kept reading the original AxisConfig objects ("strange results");
* the Alpha/Beta axis tabs must not duplicate (and clobber on Apply) the
  calibration values owned by the Calibration tab;
* motion limits are driver-config defaults, not per-session dialog fields;
* new rig defaults (alpha +/-29 deg, beta +/-25 deg, tolerance 0.01 deg)
  flow through to the HAL AxisSpec;
* alpha+beta points issue ONE synchronous CrescentDrive.move_to;
* the embedded standalone CrescentPanel operates the adapter's OWN drive
  (never a second drive/Modbus connection).
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication                 # noqa: E402

from freestream.adapters.crescent import CrescentAdapter  # noqa: E402
from freestream.app.device_config import DeviceConfigDialog  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


def _wait(cond, timeout=20.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _teardown(dlg):
    dlg._pump.stop()
    dlg._stop_device_panel()


@pytest.fixture()
def adapter():
    a = CrescentAdapter(sim=True)
    a.connect()
    yield a
    a.disconnect()


# -- new rig defaults reach the HAL specs ---------------------------------
def test_axis_specs_reflect_rig_defaults():
    a = CrescentAdapter(sim=True)
    specs = {s.name: s for s in a.axes()}
    assert specs["alpha"].min == -29.0 and specs["alpha"].max == 29.0
    assert specs["beta"].min == -25.0 and specs["beta"].max == 25.0
    assert specs["alpha"].tolerance == 0.01
    assert specs["beta"].tolerance == 0.01


# -- regression: loaded config must rebind the RUNNING drive --------------
def test_apply_config_dict_rebinds_running_drive(app, adapter):
    """Saved/loaded driver configs previously replaced ``adapter.config``'s
    axis objects without CrescentDrive.set_config -- the drive kept the old
    calibration and the load was silently ignored."""
    data = adapter.config_dict()
    data["alpha"].update(angle_high=2.0, encoder_high=0,
                         clicks_per_degree=200.0, calibrated=True)
    adapter.apply_config_dict(data)

    # the drive must hold the SAME AxisConfig objects the adapter edits
    assert adapter.driver._alpha.cfg is adapter.config.alpha
    assert adapter.driver._beta.cfg is adapter.config.beta
    assert adapter.driver._alpha.cfg.clicks_per_degree == 200.0

    # a subsequent move must run under the NEW calibration: alpha=2.5 deg
    # maps to encoder round(0 - (2.0 - 2.5) * 200) = +100 counts (the old
    # default cal would park near 737)
    adapter.move_to(alpha=2.5)
    assert _wait(adapter.settled, 30.0), "move never settled"
    assert adapter.positions()["alpha"] == pytest.approx(2.5, abs=0.05)
    assert adapter.driver.state()["Alpha"]["encoder"] == pytest.approx(
        100, abs=5), "move did not use the loaded calibration mapping"


def test_dialog_cancel_keeps_drive_bound(app, adapter):
    """Dialog Cancel restores the snapshot via apply_config_dict -- after
    which cal-panel-style in-place edits must STILL reach the drive."""
    dlg = DeviceConfigDialog(adapter)
    dlg.reject()
    assert adapter.driver._alpha.cfg is adapter.config.alpha
    # a cal edit on the adapter's config must be the drive's cal too
    adapter.config.alpha.calibrate_offset(
        1.5, adapter.driver.state()["Alpha"]["encoder"])
    assert adapter.driver._alpha.cfg.angle_high == 1.5, \
        "post-cancel calibration edit ignored by the drive"
    assert adapter.driver.state()["Alpha"]["calibrated"]


# -- regression: axis tabs must not clobber the calibration tab -----------
def test_dialog_apply_does_not_clobber_calibration(app, adapter):
    """Two-point cal (what the Calibration tab performs on the live config)
    followed by dialog Apply previously re-wrote the STALE axis-tab widget
    values over the fresh calibration."""
    dlg = DeviceConfigDialog(adapter)
    try:
        cpd = adapter.config.alpha.calibrate_two_point(0.0, 0, 2.0, 500)
        dlg._apply()
        alpha = adapter.config.alpha
        assert alpha.clicks_per_degree == pytest.approx(cpd)   # 250
        assert alpha.angle_high == pytest.approx(2.0)
        assert alpha.encoder_high == 500
        assert alpha.calibrated
        # the running drive sees it too (same objects, rebound on Apply)
        assert adapter.driver.state()["Alpha"]["calibrated"]
    finally:
        _teardown(dlg)


def test_dialog_has_no_limit_or_duplicate_cal_editors(app):
    """Motion limits are driver defaults (not per-session UI) and the
    calibration values have exactly ONE editor: the Calibration tab."""
    a = CrescentAdapter(sim=True)
    dlg = DeviceConfigDialog(a)
    try:
        fields = set()
        for form in dlg._forms:
            fields |= set(form.fields())
        banned = {"min_deg", "max_deg", "tolerance_deg", "angle_high",
                  "encoder_high", "clicks_per_degree", "calibrated"}
        assert not (fields & banned), f"duplicated editors: {fields & banned}"
    finally:
        _teardown(dlg)


# -- synchronous alpha+beta moves at the adapter level --------------------
def test_adapter_synchronous_move(adapter):
    """One adapter.move_to(alpha=..., beta=...) -> ONE drive.move_to -> both
    axes in motion in the same control tick (mirrors the driver-level test
    devices/tests/test_crescent.py::test_synchronous_move_both_axes)."""
    handle = adapter.move_to(alpha=4.0, beta=-3.0)
    assert handle.targets == {"alpha": 4.0, "beta": -3.0}
    time.sleep(0.3)
    st = adapter.driver.state()
    assert st["Alpha"]["moving"] and st["Beta"]["moving"], \
        "axes did not move simultaneously"
    assert _wait(adapter.settled, 30.0), "sync move never settled"
    pos = adapter.positions()
    assert pos["alpha"] == pytest.approx(4.0, abs=0.2)
    assert pos["beta"] == pytest.approx(-3.0, abs=0.2)


# -- embedded standalone CrescentPanel ------------------------------------
def test_embedded_panel_shares_adapters_drive(app, adapter):
    """The dialog embeds the devices-app CrescentPanel wired to the SAME
    CrescentDrive -- never a second drive/Modbus connection -- with the
    Connection row hidden (Freestream owns the lifecycle)."""
    dlg = DeviceConfigDialog(adapter)
    try:
        panel = dlg._device_panel
        assert panel is not None, "crescent dialog lacks the device panel"
        assert panel.device is adapter.driver
        assert panel.conn_group.isHidden()
        # the exact standalone GUI: motion cards + sync group + cal tab
        assert panel.alpha_card is not None and panel.beta_card is not None
        assert panel.cal_panel is not None
    finally:
        _teardown(dlg)


def test_embedded_panel_jog_and_move_both(app, adapter):
    dlg = DeviceConfigDialog(adapter)
    try:
        panel = dlg._device_panel
        panel._refresh_ui()                    # sync connected-state UI
        assert panel.sync_btn.isEnabled()
        # target spins picked up the driver-config limits
        assert panel.sync_alpha.minimum() == -29.0
        assert panel.sync_beta.maximum() == 25.0

        # hold-to-run jog through the panel moves the adapter's axis
        p0 = adapter.positions()["alpha"]
        panel.alpha_card.jog_plus.pressed.emit()
        time.sleep(0.6)
        panel.alpha_card.jog_plus.released.emit()
        assert _wait(lambda: not adapter.driver.state()["Alpha"]["jogging"],
                     2.0)
        assert adapter.positions()["alpha"] > p0 + 0.02, "jog did not move"

        # Move Both issues ONE synchronous drive.move_to
        panel.sync_alpha.setValue(1.0)
        panel.sync_beta.setValue(-1.0)
        panel.sync_btn.click()
        time.sleep(0.3)
        st = adapter.driver.state()
        assert st["Alpha"]["moving"] and st["Beta"]["moving"], \
            "Move Both did not start both axes together"
        assert _wait(adapter.settled, 30.0)
        assert adapter.positions()["alpha"] == pytest.approx(1.0, abs=0.2)
        assert adapter.positions()["beta"] == pytest.approx(-1.0, abs=0.2)
    finally:
        _teardown(dlg)


def test_embedded_cal_tab_applies_to_running_drive(app, adapter):
    """Apply on the embedded calibration tab -> the running drive updates
    immediately: displayed angles recompute, limits/tolerance unchanged,
    and the next move lands under the new calibration."""
    dlg = DeviceConfigDialog(adapter)
    try:
        ac = dlg._device_panel.cal_panel.alpha_cal
        ac.cpd.setValue(200.0)
        ac.angle_high.setValue(2.0)
        ac.encoder_high.setValue(0)
        ac._apply_constants()
        dlg._apply()                       # the dialog's Apply path

        # the RUNNING drive holds the new constants (same objects, rebound)
        assert adapter.config.alpha.clicks_per_degree == 200.0
        assert adapter.driver._alpha.cfg.clicks_per_degree == 200.0
        assert adapter.driver._alpha.cfg.angle_high == 2.0

        # the next move runs under the NEW mapping: 2.5 deg -> +100 counts
        # (the sim emulator regenerates its encoder through the same cal,
        # so the count target -- not a displayed-angle jump -- is the
        # observable proof; on hardware the encoder is physical)
        adapter.move_to(alpha=2.5)
        assert _wait(adapter.settled, 30.0)
        assert adapter.positions()["alpha"] == pytest.approx(2.5, abs=0.05)
        assert adapter.driver.state()["Alpha"]["encoder"] == pytest.approx(
            100, abs=5)
        # AxisSpec limits/tolerance untouched by calibration
        specs = {s.name: s for s in adapter.axes()}
        assert specs["alpha"].min == -29.0 and specs["alpha"].max == 29.0
        assert specs["alpha"].tolerance == 0.01
    finally:
        _teardown(dlg)
