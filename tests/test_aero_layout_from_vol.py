"""Live/Results aero: the .vol's declared balance type decides the layout.

A force balance ('5 Force/1 Moment') configured under the Moment layout
read CL ~2.75x low on the Forces page and in Process & Report
(F16 check model, 2026-10-06).
"""
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from freestream.aero import compute_aero, declared_balance_config

VOL = (Path(__file__).resolve().parents[2] / "Streamlined" / "CalFiles"
       / "2025_06_06_2 100 lb.vol")


@pytest.mark.parametrize("text,expected", [
    ("5 Force/1 Moment", "Force"), ("5 Moment/1 Force", "Moment"),
    ("1-Force / 5-Moment", "Moment"), ("", None)])
def test_declared_balance_config(text, expected):
    cal = SimpleNamespace(description=SimpleNamespace(balance_type=text))
    assert declared_balance_config(cal) == expected


@pytest.mark.skipif(not VOL.exists(), reason="100 lb .vol not present")
def test_moment_layout_on_a_force_balance_is_overridden():
    from freestream.aero import load_balance_cal
    cal = load_balance_cal(VOL, "Linear")
    rng = np.random.default_rng(1)
    # moment-layout channel NAMES, as the October 2026 files carry them
    raw = {k: 2e-3 + 1e-5 * rng.standard_normal(200)
           for k in ("AftPitch", "AftYaw", "FwdPitch", "FwdYaw")}
    raw.update(Axial=np.full(200, 1e-4), Roll=np.full(200, 1e-5),
               Excitation=np.full(200, 9.86))
    as_moment = compute_aero(raw, cal, 8.0, 0.0, "Moment")
    as_force = compute_aero(raw, cal, 8.0, 0.0, "Force")
    assert as_moment.balance_config == "Force"
    assert "overridden" in as_moment.config_note
    assert as_moment.means()["Lift"] == pytest.approx(
        as_force.means()["Lift"])
    assert as_force.config_note == ""
