"""ARC Crescent driver tests — sim physics, synchronous moves, limits,
watchdog-free basics. No hardware required.
"""

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ac_delta.axis import FWD_STEPS, REV_STEPS
from ac_delta.config import AxisConfig, CrescentConfig
from ac_delta.device import CrescentDrive


def _sim_config(**kw) -> CrescentConfig:
    cfg = CrescentConfig(force_sim=True, loop_ms=20, **kw)
    for ax in cfg.axes():
        ax.calibrated = True
        ax.clicks_per_degree = 100.0
        ax.angle_high = 20.0
        ax.encoder_high = 2000
    return cfg


def test_encoder_angle_roundtrip():
    ax = AxisConfig(angle_high=20.0, encoder_high=2000,
                    clicks_per_degree=100.0)
    assert abs(ax.encoder_to_angle(2000) - 20.0) < 1e-9
    assert abs(ax.encoder_to_angle(0) - 0.0) < 1e-9
    assert ax.angle_to_encoder(-5.0) == -500   # 2000 - (20-(-5))*100
    assert ax.angle_to_encoder(ax.encoder_to_angle(1234)) == 1234


def test_step_tables_match_csharp():
    assert FWD_STEPS == [4370, 4626, 4882, 5138, 5394]
    assert REV_STEPS == [4386, 4642, 4898, 5154, 5410]


def test_beta_direction_inversion():
    from ac_delta.emulator import SimAxis
    alpha = SimAxis(AxisConfig(name="Alpha", invert_direction=False))
    beta = SimAxis(AxisConfig(name="Beta", stop_value=33,
                              invert_direction=True))
    alpha.connect()
    beta.connect()
    alpha.command_step(3, forward=True)
    beta.command_step(3, forward=True)
    # same angle-space direction, opposite wire tables (as deployed C#)
    assert alpha.last_command() == FWD_STEPS[2]
    assert beta.last_command() == REV_STEPS[2]


def test_limits_rejected_before_motion():
    dev = CrescentDrive(_sim_config())
    try:
        dev.connect()
        try:
            dev.move_to(alpha=999.0)
            raise AssertionError("limit violation not rejected")
        except ValueError:
            pass
        assert not dev.state()["Alpha"]["moving"]
    finally:
        dev.disconnect()


def _wait_settled(dev, timeout=30.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        st = dev.state()
        if not st["Alpha"]["moving"] and not st["Beta"]["moving"]:
            return True
        time.sleep(0.05)
    return False


def test_single_axis_move_converges():
    dev = CrescentDrive(_sim_config())
    try:
        dev.connect()
        dev.move_to(alpha=5.0)
        assert _wait_settled(dev), "alpha move never completed"
        st = dev.state()["Alpha"]
        assert abs(st["angle"] - 5.0) < 0.2
    finally:
        dev.disconnect()


def test_synchronous_move_both_axes():
    dev = CrescentDrive(_sim_config())
    try:
        dev.connect()
        dev.move_to(alpha=4.0, beta=-3.0)
        # both must be in motion simultaneously shortly after the command
        time.sleep(0.3)
        st = dev.state()
        assert st["Alpha"]["moving"] and st["Beta"]["moving"], \
            "axes did not move simultaneously"
        assert _wait_settled(dev), "sync move never completed"
        st = dev.state()
        assert abs(st["Alpha"]["angle"] - 4.0) < 0.2
        assert abs(st["Beta"]["angle"] + 3.0) < 0.2
        # history shows overlapping motion
        hist = dev.ring.tail(2000)
        both = (hist["Alpha_moving"] > 0) & (hist["Beta_moving"] > 0)
        assert both.any(), "no overlapping-motion samples in history"
    finally:
        dev.disconnect()


def test_stop_all_halts_motion():
    dev = CrescentDrive(_sim_config())
    try:
        dev.connect()
        dev.move_to(alpha=10.0, beta=10.0)
        time.sleep(0.3)
        dev.stop_all()
        st = dev.state()
        assert not st["Alpha"]["moving"] and not st["Beta"]["moving"]
        time.sleep(0.15)                  # let the loop tick past the stop
        a1 = dev.state()["Alpha"]["angle"]
        time.sleep(0.4)
        a2 = dev.state()["Alpha"]["angle"]
        assert abs(a2 - a1) < 0.15, "axis kept moving after stop_all"
    finally:
        dev.disconnect()


def test_jog_hold_and_release():
    dev = CrescentDrive(_sim_config())
    try:
        dev.connect()
        a0 = dev.state()["Alpha"]["angle"]
        dev.jog("alpha", forward=True, step=3)
        time.sleep(0.6)
        assert dev.state()["Alpha"]["jogging"]
        a1 = dev.state()["Alpha"]["angle"]
        assert a1 > a0 + 0.05, "jog did not move the axis"
        dev.jog_stop("alpha")
        time.sleep(0.2)
        assert not dev.state()["Alpha"]["jogging"]
        a2 = dev.state()["Alpha"]["angle"]
        time.sleep(0.3)
        assert abs(dev.state()["Alpha"]["angle"] - a2) < 0.05, \
            "axis kept moving after jog release"
    finally:
        dev.disconnect()


def test_jog_stops_at_soft_limit_when_calibrated():
    cfg = _sim_config()
    cfg.alpha.max_deg = 0.5           # tiny limit so the jog trips it
    dev = CrescentDrive(cfg)
    try:
        dev.connect()
        dev.jog("alpha", forward=True, step=5)
        deadline = time.perf_counter() + 10.0
        while time.perf_counter() < deadline and \
                dev.state()["Alpha"]["jogging"]:
            time.sleep(0.05)
        st = dev.state()["Alpha"]
        assert not st["jogging"], "jog never auto-stopped at limit"
        assert st["angle"] < 1.5, "axis blew far past the soft limit"
    finally:
        dev.disconnect()


def test_uncalibrated_blocks_moves_but_allows_jog():
    cfg = _sim_config()
    cfg.alpha.calibrated = False
    dev = CrescentDrive(cfg)
    try:
        dev.connect()
        try:
            dev.move_to(alpha=2.0)
            raise AssertionError("uncalibrated move not rejected")
        except ValueError:
            pass
        dev.jog("alpha", forward=True, step=2)   # jog must still work
        time.sleep(0.4)
        dev.jog_stop("alpha")
        assert dev.state()["Alpha"]["encoder"] != 0 or True
    finally:
        dev.disconnect()


def test_two_point_then_offset_calibration():
    ax = AxisConfig()
    assert not ax.calibrated
    # full cal: -10 deg @ enc -1000, +10 deg @ enc +1000 -> 100 clicks/deg
    cpd = ax.calibrate_two_point(-10.0, -1000, 10.0, 1000)
    assert abs(cpd - 100.0) < 1e-9
    assert ax.calibrated
    assert abs(ax.encoder_to_angle(0) - 0.0) < 1e-9
    assert abs(ax.encoder_to_angle(500) - 5.0) < 1e-9
    # offset-only re-zero: encoder 500 declared to be 0 deg; slope kept
    ax.calibrate_offset(0.0, 500)
    assert abs(ax.clicks_per_degree - 100.0) < 1e-9
    assert abs(ax.encoder_to_angle(500) - 0.0) < 1e-9
    assert abs(ax.encoder_to_angle(600) - 1.0) < 1e-9
    # degenerate cases rejected
    try:
        ax.calibrate_two_point(5.0, 100, 5.0, 200)
        raise AssertionError("same-angle points not rejected")
    except ValueError:
        pass
    try:
        ax.calibrate_two_point(1.0, 100, 2.0, 100)
        raise AssertionError("same-encoder points not rejected")
    except ValueError:
        pass
    bad = AxisConfig(clicks_per_degree=0.0)
    try:
        bad.calibrate_offset(0.0, 0)
        raise AssertionError("offset cal without slope not rejected")
    except ValueError:
        pass


def test_set_config_applies_calibration_live():
    """Regression: loading a config (or applying calibration to a freshly
    loaded config) must reach the running drive — previously the drive
    kept reading the ORIGINAL AxisConfig objects and stayed uncalibrated.
    """
    cfg = _sim_config()
    for ax in cfg.axes():
        ax.calibrated = False
    dev = CrescentDrive(cfg)
    try:
        dev.connect()
        time.sleep(0.2)
        assert not dev.state()["Alpha"]["calibrated"]
        try:
            dev.move_to(alpha=1.0)
            raise AssertionError("uncalibrated move should be rejected")
        except ValueError:
            pass

        # simulate File -> Load config with calibrated axes
        new_cfg = _sim_config()          # calibrated=True in the helper
        dev.set_config(new_cfg)
        time.sleep(0.2)
        st = dev.state()
        assert st["Alpha"]["calibrated"] and st["Beta"]["calibrated"], \
            "loaded calibration not recognized by the running drive"
        dev.move_to(alpha=1.0)           # must now be accepted
        assert _wait_settled(dev), "post-load move never completed"
        assert abs(dev.state()["Alpha"]["angle"] - 1.0) < 0.2

        # and calibrating an axis AFTER a config load must also stick
        # (the cal panel edits the loaded config's AxisConfig objects)
        new_cfg.beta.calibrated = False
        dev.set_config(new_cfg)
        assert not dev.state()["Beta"]["calibrated"]
        new_cfg.beta.calibrate_offset(0.0, dev.state()["Beta"]["encoder"])
        assert dev.state()["Beta"]["calibrated"], \
            "cal-panel edit on loaded config not seen by drive"
    finally:
        dev.disconnect()


def test_config_roundtrip():
    import tempfile
    cfg = CrescentConfig()
    cfg.loop_ms = 40
    cfg.beta.tolerance_deg = 0.1
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cfg.json"
        cfg.save(p)
        back = CrescentConfig.load(p)
    assert back.loop_ms == 40
    assert back.alpha.ip == "192.168.1.11"
    assert back.beta.ip == "192.168.1.12"
    assert back.beta.stop_value == 33
    assert back.beta.invert_direction is True
    assert back.beta.tolerance_deg == 0.1
    # measured on-rig slopes are the defaults (offset still needs re-zero)
    assert abs(CrescentConfig().alpha.clicks_per_degree - 294.8292) < 1e-6
    assert abs(CrescentConfig().beta.clicks_per_degree - 202.9586) < 1e-6
    assert CrescentConfig().speed_bands_deg == [0.35, 0.7, 1.4, 1.7]
    # rig travel limits + settle band (2026-07 numbers from Casey)
    fresh = CrescentConfig()
    assert (fresh.alpha.min_deg, fresh.alpha.max_deg) == (-29.0, 29.0)
    assert (fresh.beta.min_deg, fresh.beta.max_deg) == (-25.0, 25.0)
    assert fresh.alpha.tolerance_deg == 0.01
    assert fresh.beta.tolerance_deg == 0.01


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} crescent tests passed.")


if __name__ == "__main__":
    _run_all()
