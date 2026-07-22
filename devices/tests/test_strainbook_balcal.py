"""Balance-calibration (.vol) tests — parsing, cal matrices, BRF forces,
load-limit utilization. Uses a real Streamlined .vol file; skips those
tests if the CalFiles folder is absent.
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strainbook_616 import balcal

CALDIR = Path(__file__).resolve().parents[2] / "Streamlined" / "CalFiles"
VOL = CALDIR / "2025_06_06_2 100 lb.vol"


def test_read_vol_and_fit():
    if not VOL.exists():
        print("  (skipped — Streamlined CalFiles not present)")
        return
    cal = balcal.read_vol_file(str(VOL))
    assert len(cal.force_channels) == 6, cal.force_channels
    assert cal.force.shape[1] == 6
    assert cal.volts.shape == cal.force.shape
    assert cal.max_loads.values, "no max loads parsed"
    assert cal.distances.values, "no distances parsed"

    balcal.calc_coeffs(cal, "Linear")
    assert cal.coeffs.shape == (6, 6)
    assert cal.r_squared.min() > 0.95, f"poor fit: {cal.r_squared}"

    balcal.calc_coeffs(cal, "Quadratic")
    assert cal.coeffs.shape == (12, 6)
    balcal.calc_coeffs(cal, "Cubic")
    assert cal.coeffs.shape == (18, 6)


def test_brf_forces_zero_volts_gives_zero():
    if not VOL.exists():
        print("  (skipped — Streamlined CalFiles not present)")
        return
    cal = balcal.calc_coeffs(balcal.read_vol_file(str(VOL)), "Linear")
    n = 50
    zeros = np.zeros(n)
    raw = {name: zeros for name in
           ("N1", "N2", "Y1", "Y2", "Axial", "Roll")}
    raw["Excitation"] = np.full(n, 10.0)
    brf = balcal.calc_brf_forces(raw, cal, balance_config="Force")
    for attr in ("Fx", "Fy", "Fz", "Mx", "My", "Mz"):
        assert np.allclose(getattr(brf, attr), 0.0, atol=1e-9)
    assert brf.elements.shape == (n, 6)


def test_brf_reproduces_calibration_points():
    """Feeding the cal file's own voltage rows back through the pipeline
    must reproduce the applied calibration loads (within fit error)."""
    if not VOL.exists():
        print("  (skipped — Streamlined CalFiles not present)")
        return
    cal = balcal.calc_coeffs(balcal.read_vol_file(str(VOL)), "Linear")
    # cal.volts are already excitation-normalized; use excitation=1
    names = ["N1", "N2", "Y1", "Y2", "Axial", "Roll"]
    raw = {n: cal.volts[:, i] for i, n in enumerate(names)}
    raw["Excitation"] = np.ones(cal.volts.shape[0])
    brf = balcal.calc_brf_forces(raw, cal, balance_config="Force")
    # element estimates match the least-squares force estimates exactly
    assert np.allclose(brf.elements, cal.force_est, atol=1e-9)
    # and the applied loads within the fit bias (allow 3x RMSE + epsilon)
    err = np.abs(brf.elements - cal.force)
    assert (err.max(axis=0) < 3 * cal.bias + 0.5).all(), \
        f"max errors {err.max(axis=0)} vs bias {cal.bias}"


def test_element_utilization_and_overstress():
    cal = balcal.BalanceCalibration()
    cal.force_channels = ["N1", "N2", "Y1", "Y2", "Axial", "Roll"]
    cal.max_loads.values = {"N1": 100.0, "N2": 100.0, "Y1": 50.0,
                            "Y2": 50.0, "Ax": 25.0, "Roll": 10.0}
    elements = np.array([[50.0, -120.0, 10.0, 0.0, 5.0, 1.0],
                         [40.0, -80.0, 12.0, 0.0, 30.0, 1.0]])
    util = balcal.element_utilization(cal, elements)
    assert abs(util["N1"] - 0.5) < 1e-9
    assert abs(util["N2"] - 1.2) < 1e-9          # overstressed
    assert abs(util["Y1"] - 0.24) < 1e-9
    assert abs(util["Axial"] - 30.0 / 25.0) < 1e-9   # 'Ax' fuzzy-matched
    assert max(util.values()) >= 1.0


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} balcal tests passed.")


if __name__ == "__main__":
    _run_all()
