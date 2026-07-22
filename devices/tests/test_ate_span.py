"""Model-span configuration (full vs ½ span) — driver-level tests.

The driver OWNS the alpha/beta → physical-drive mapping:

* full (default): alpha → INCIDENCE drive (GOTO_INC_POS, −10..45°),
  beta → YAW drive (GOTO_YAW_POS, −90..90°) — unchanged behaviour.
* half: alpha → the YAW drive (½-span model on the turntable), beta is
  REJECTED, and the incidence drive is NEVER commanded.

Uses the in-process sim (OgiSimCore) so the tests can assert exactly
which physical drive a logical command reached.
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ate_balance import protocol as P
from ate_balance.config import AteConfig, SPAN_CONFIGS, SPAN_FULL, SPAN_HALF
from ate_balance.device import AteBalanceDevice


def _sim_device(span: str) -> AteBalanceDevice:
    dev = AteBalanceDevice(AteConfig(force_sim=True, span_config=span))
    dev.connect()
    return dev


# ── config field ─────────────────────────────────────────────────────────
def test_span_config_default_is_full():
    assert AteConfig().span_config == SPAN_FULL
    assert SPAN_CONFIGS == ("full", "half")


def test_span_config_json_round_trip(tmp_path):
    cfg = AteConfig(span_config=SPAN_HALF)
    path = tmp_path / "ate.json"
    cfg.save(path)
    loaded = AteConfig.load(path)
    assert loaded.span_config == SPAN_HALF
    assert loaded.to_dict()["span_config"] == SPAN_HALF
    # old configs without the key default to full span
    assert AteConfig.from_dict({"ogi_ip": "10.0.0.1"}).span_config == \
        SPAN_FULL


def test_span_config_validated():
    with pytest.raises(ValueError, match="span_config"):
        AteConfig(span_config="both")
    with pytest.raises(ValueError, match="span_config"):
        AteConfig.from_dict({"span_config": "semi"})


# ── full span: mapping unchanged ─────────────────────────────────────────
def test_full_span_mapping_unchanged():
    dev = _sim_device(SPAN_FULL)
    try:
        assert dev.span_config == SPAN_FULL and not dev.half_span
        assert dev.alpha_limits() == P.INC_LIMITS_DEG
        assert dev.beta_limits() == P.YAW_LIMITS_DEG

        dev.goto_alpha(5.0)                    # alpha → INCIDENCE drive
        assert dev._core.inc_pos == pytest.approx(5.0)
        assert dev._core.yaw_pos == pytest.approx(0.0)

        dev.goto_beta(10.0)                    # beta → YAW drive
        assert dev._core.yaw_pos == pytest.approx(10.0)
        assert dev._core.inc_pos == pytest.approx(5.0)

        assert dev.map_positions(10.0, 5.0) == \
            {"alpha": 5.0, "beta": 10.0}
        assert dev.logical_axis_for_reply(P.RSP_INC_COMPLETE) == "alpha"
        assert dev.logical_axis_for_reply(P.RSP_YAW_COMPLETE) == "beta"
    finally:
        dev.disconnect()


def test_full_span_alpha_limits_are_incidence():
    dev = _sim_device(SPAN_FULL)
    try:
        with pytest.raises(ValueError, match="outside limits"):
            dev.goto_alpha(50.0)               # beyond inc max 45°
        with pytest.raises(ValueError, match="outside limits"):
            dev.goto_beta(95.0)                # beyond yaw max 90°
        assert dev._core.inc_pos == 0.0 and dev._core.yaw_pos == 0.0
    finally:
        dev.disconnect()


# ── half span: alpha is the yaw drive, no beta, incidence untouched ─────
def test_half_span_alpha_commands_yaw_never_incidence():
    dev = _sim_device(SPAN_HALF)
    try:
        assert dev.half_span
        assert dev.alpha_limits() == P.YAW_LIMITS_DEG
        assert dev.beta_limits() is None

        # 60° is legal on the yaw drive but ILLEGAL on the incidence
        # drive — proves alpha routed to yaw
        dev.goto_alpha(60.0)
        assert dev._core.yaw_pos == pytest.approx(60.0)
        assert dev._core.inc_pos == pytest.approx(0.0), \
            "incidence drive was commanded in ½-span"
    finally:
        dev.disconnect()


def test_half_span_rejects_beta_and_enforces_yaw_limits():
    dev = _sim_device(SPAN_HALF)
    try:
        with pytest.raises(ValueError, match="beta rejected"):
            dev.goto_beta(5.0)
        with pytest.raises(ValueError, match="outside limits"):
            dev.goto_alpha(95.0)               # yaw drive limit ±90°
        assert dev._core.yaw_pos == 0.0 and dev._core.inc_pos == 0.0
    finally:
        dev.disconnect()


def test_half_span_readback_mapping():
    dev = _sim_device(SPAN_HALF)
    try:
        # position readback: alpha reads from the YAW position; no beta
        assert dev.map_positions(30.0, 5.0) == {"alpha": 30.0}
        # YAW replies belong to alpha; INC replies belong to no axis
        assert dev.logical_axis_for_reply(P.RSP_YAW_COMPLETE) == "alpha"
        assert dev.logical_axis_for_reply(P.RSP_YAW_MOVING) == "alpha"
        assert dev.logical_axis_for_reply(P.RSP_INC_COMPLETE) is None
    finally:
        dev.disconnect()


def test_raw_primitives_stay_untouched_in_half_span():
    """Manual per-drive control (the app's Motion tab) keeps working —
    the raw goto primitives bypass the logical mapping."""
    dev = _sim_device(SPAN_HALF)
    try:
        dev.goto_inc(5.0)                      # explicit manual command
        assert dev._core.inc_pos == pytest.approx(5.0)
        dev.goto_yaw(-20.0)
        assert dev._core.yaw_pos == pytest.approx(-20.0)
    finally:
        dev.disconnect()
