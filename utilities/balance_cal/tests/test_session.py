"""Session model: orientations, moment-arm logic, guide-image names."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from balcal_gui.session import (BalanceKind, CalSession, TestPoint,
                                elements_for)

IMAGES = Path(__file__).resolve().parents[1] / "FB_Cal_GUI"


def test_orientation_keys_force():
    s = CalSession(kind=BalanceKind.FORCE)
    keys = [o.key for o in s.orientations]
    assert keys[:4] == ["N1_pos", "N1_neg", "N2_pos", "N2_neg"]
    assert keys[-2:] == ["Mx_pos", "Mx_neg"]
    assert len(keys) == 12


def test_moment_arm_force_balance():
    s = CalSession(kind=BalanceKind.FORCE)
    assert s.moment_arm("N1_pos") == 1.0
    assert s.moment_arm("Ax_neg") == 1.0
    # roll defaults to 2 in, or the entered roll-arm distance
    assert s.moment_arm("Mx_pos") == 2.0
    s.distances["roll_arm"] = 3.5
    assert s.moment_arm("Mx_pos") == 3.5


def test_moment_arm_is_station_separation():
    """Weight hangs at the opposite station: arm = dx1 + dx2 (sum of
    both stations' distances to the balance center), NOT 2x either
    single distance — asymmetric balances must use the true sum."""
    s = CalSession(kind=BalanceKind.MOMENT)
    s.distances.update({"x1": 1.287, "y1": 1.25,      # pitch pair
                        "x2": 1.2645, "y2": 1.2646})  # yaw pair
    assert s.moment_arm("Aft_Pitch_pos") == 1.287 + 1.25
    assert s.moment_arm("Fwd_Pitch_neg") == 1.287 + 1.25
    assert s.moment_arm("Aft_Yaw_pos") == 1.2645 + 1.2646
    assert s.moment_arm("Fwd_Yaw_neg") == 1.2645 + 1.2646
    assert s.moment_arm("Ax_pos") == 1.0


def test_moment_arm_needs_both_pair_distances():
    s = CalSession(kind=BalanceKind.MOMENT)
    s.distances["x1"] = 1.287                  # fwd pitch missing
    assert s.moment_arm("Aft_Pitch_pos") == 1.0
    warn = s.moment_arm_warning("Aft_Pitch_pos")
    assert warn and "x1 + y1" in warn
    s.distances["y1"] = 1.25
    assert s.moment_arm("Aft_Pitch_pos") == 1.287 + 1.25
    assert s.moment_arm_warning("Aft_Pitch_pos") is None


def test_guide_images_exist_for_force_balance():
    s = CalSession(kind=BalanceKind.FORCE)
    for o in s.orientations:
        assert (IMAGES / o.image_name(s.kind)).exists(), \
            o.image_name(s.kind)


def test_guide_images_exist_for_moment_balance():
    s = CalSession(kind=BalanceKind.MOMENT)
    for o in s.orientations:
        assert (IMAGES / o.image_name(s.kind)).exists(), \
            o.image_name(s.kind)


def test_add_remove_points():
    s = CalSession()
    p = TestPoint(load=5.0, volts=[0.0] * 6, excitation=10.0)
    s.add_point("N1_pos", p)
    s.add_point("N1_pos", p)
    assert s.point_count() == 2
    s.remove_point("N1_pos", 0)
    assert s.point_count() == 1


def test_channel_names_match_ni_driver_convention():
    force = [el.channel for el in elements_for(BalanceKind.FORCE)]
    moment = [el.channel for el in elements_for(BalanceKind.MOMENT)]
    assert force == ["N1", "N2", "Y1", "Y2", "Axial", "Roll"]
    assert moment == ["AftPitch", "AftYaw", "FwdPitch", "FwdYaw",
                      "Axial", "Roll"]
