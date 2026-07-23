"""SIM-mode tests for the LswtTunnelAdapter (North LSWT ABB ACS530 fan).

Built as the DeviceManager would (``cls(sim=True)``), exercised against
the driver's SimAcs530 first-order fan plant — no hardware, no Qt.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "devices"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from freestream.adapters.lswt import (LswtTunnelAdapter,       # noqa: E402
                                      RPM_PER_HZ)
from freestream.hal import SetpointDevice, capabilities        # noqa: E402
from lswt import calibration                                   # noqa: E402


def _fast(a: LswtTunnelAdapter) -> LswtTunnelAdapter:
    """Shrink the sim plant/ramp so tests settle in a couple seconds."""
    a.config.ramp_hz_per_s = 100.0
    a.config.sim_tau_s = 0.1
    a.config.poll_s = 0.02
    return a


def _wait_at_target(a, timeout=10.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if a.at_target():
            return True
        time.sleep(0.05)
    return False


def test_identity_and_capabilities():
    a = LswtTunnelAdapter(sim=True)
    assert a.id == "lswt"
    assert "North LSWT" in a.label
    assert a.config.tunnel == "north"          # new mode = North tunnel
    assert isinstance(a, SetpointDevice)
    assert capabilities(a) == ["setpoint"]     # NOT streaming/balance
    assert a.settings_dialog_path == "lswt.app.settings_dialog:SettingsDialog"


def test_sim_connect_status_and_hz_setpoint():
    a = _fast(LswtTunnelAdapter(sim=True))
    a.connect()
    try:
        assert a.connected and a.sim
        assert a.status().ok
        a.set_target(hz=12.0)                  # sim auto-starts the fan
        assert _wait_at_target(a), "sim fan never reached 12 Hz"
        rb = a.readback()
        assert rb["hz_set"] == pytest.approx(12.0)
        assert rb["hz"] == pytest.approx(12.0, abs=1.0)
        # rpm keys are the documented Hz*60 equivalence
        assert rb["rpm_set"] == pytest.approx(12.0 * RPM_PER_HZ)
        assert rb["rpm"] == pytest.approx(rb["hz"] * RPM_PER_HZ)
        # velocity published through the measured calibration
        assert rb["velocity_fps"] == pytest.approx(
            calibration.hz_to_fps(rb["hz"]), abs=1e-6)
    finally:
        a.disconnect()


def test_rpm_setpoint_maps_to_hz():
    """rpm ⇄ Hz is 1:1 for the LSWT (rig-corrected 2026-07-23): the
    entered value IS the drive Hz, matching the standalone app — a
    commanded 10 runs the fan at 10 Hz, NOT 0.17 (the old ×60 bug)."""
    a = _fast(LswtTunnelAdapter(sim=True))
    a.connect()
    try:
        a.set_target(rpm=10.0)                 # 1:1 → 10 Hz
        assert a.readback()["hz_set"] == pytest.approx(10.0)
        assert a.readback()["rpm_set"] == pytest.approx(10.0)
        assert _wait_at_target(a), "sim fan never settled at 10 Hz"
    finally:
        a.disconnect()


def test_velocity_setpoint_maps_through_calibration():
    a = _fast(LswtTunnelAdapter(sim=True))
    a.connect()
    try:
        a.set_target(velocity=50.0)
        assert a.readback()["hz_set"] == pytest.approx(
            calibration.fps_to_hz(50.0), abs=1e-6)
    finally:
        a.disconnect()


def test_setpoint_rejects_unknown_and_ambiguous_kwargs():
    a = _fast(LswtTunnelAdapter(sim=True))
    a.connect()
    try:
        with pytest.raises(ValueError):
            a.set_target(mach=0.2)             # Mach lives in the engine
        with pytest.raises(ValueError):
            a.set_target(hz=10.0, rpm=600.0)   # exactly one setpoint kind
        with pytest.raises(ValueError):
            a.set_target()
    finally:
        a.disconnect()


def test_fan_stop_and_estop_zero_the_reference():
    a = _fast(LswtTunnelAdapter(sim=True))
    a.connect()
    try:
        a.set_target(hz=10.0)
        assert _wait_at_target(a)
        a.fan_stop()
        rb = a.readback()
        assert rb["hz_set"] == 0.0
        a.set_target(hz=5.0)
        a.estop()
        assert a.readback()["hz_set"] == 0.0
        snap = a.snapshot()
        assert not snap.fan_running
    finally:
        a.disconnect()


def test_snapshot_shape_for_dashboard():
    a = _fast(LswtTunnelAdapter(sim=True))
    a.connect()
    try:
        a.set_target(hz=6.0)
        assert _wait_at_target(a)
        snap = a.snapshot()
        # the tunnel dashboard duck-types these attrs
        assert snap.fan_running
        assert not snap.stale
        assert snap.actual_rpm == pytest.approx(snap.actual_hz * RPM_PER_HZ)
        assert snap.rpm_set == pytest.approx(6.0 * RPM_PER_HZ)
        assert snap.velocity_fps == pytest.approx(
            calibration.hz_to_fps(snap.actual_hz), abs=1e-6)
    finally:
        a.disconnect()


def test_apply_config_dict_preserves_force_sim():
    """Manager owns SIM/LIVE: a bundle saved LIVE must not flip a SIM
    session's driver (and vice versa)."""
    a = LswtTunnelAdapter(sim=True)
    data = a.config_dict()
    data["force_sim"] = False                  # bundle captured LIVE
    data["ramp_hz_per_s"] = 7.5
    a.apply_config_dict(data)
    assert a.config.force_sim is True          # session mode preserved
    assert a.config.ramp_hz_per_s == 7.5       # real settings applied
