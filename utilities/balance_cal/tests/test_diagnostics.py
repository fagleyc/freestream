"""Fit diagnostics: parity with balcal, outlier flagging, exclusion."""

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "devices"))

from balcal_gui.diagnostics import (OUTLIER_Z, diagnose,
                                    diagnostics_text)
from balcal_gui.session import BalanceKind, CalSession, TestPoint
from balcal_gui.volfile import vol_text, write_vol
from ni_usb_6351 import balcal

SENS = [0.000134, 0.000135, 0.000270, 0.000268, 0.000560, 0.000265]
EXC = 9.8633


def _session(kind=BalanceKind.FORCE) -> CalSession:
    s = CalSession(kind=kind, operator="diag", cal_date=date(2026, 7, 22),
                   serial_number="D-1", outer_diameter="0.75 in.")
    for el in s.elements:
        s.max_loads[el.name] = 100.0
    s.distances.update({"x1": 1.5, "x2": 1.5, "y1": 1.25, "y2": 1.25})
    offsets = [1e-4 * (i + 1) for i in range(6)]
    for i, el in enumerate(s.elements):
        for positive in (True, False):
            key = f"{el.name}_{'pos' if positive else 'neg'}"
            sign = 1 if positive else -1
            for load in (0, 10, 20, 30, 20, 10, 0):
                signed = sign * load
                volts = list(offsets)
                volts[i] += SENS[i] * signed * EXC
                s.add_point(key, TestPoint(load=signed, volts=volts,
                                           excitation=EXC))
    return s


def test_diagnose_matches_balcal_on_clean_data(tmp_path):
    """The in-memory assembly must reproduce the consumer fit."""
    s = _session()
    d = diagnose(s, "Linear")
    path = tmp_path / "ref.vol"
    write_vol(s, str(path))
    cal = balcal.calc_coeffs(balcal.read_vol_file(str(path)), "Linear")
    assert np.allclose(d.coeffs, cal.coeffs, rtol=1e-9)
    assert np.allclose(d.r_squared, cal.r_squared, rtol=1e-9)
    assert not d.outliers()
    assert all(sec.has_zero for sec in d.sections)


def test_outlier_flagged_and_traceable():
    s = _session()
    bad = s.points["N1_pos"][3]           # the 30-lb point
    bad.volts = list(bad.volts)
    bad.volts[0] += 0.004                 # gross voltage error (~30 lb)
    d = diagnose(s, "Linear")
    out = d.outliers()
    assert out, "injected gross point not flagged"
    worst = max(out, key=lambda p: abs(p.zscore))
    assert (worst.key, worst.index) == ("N1_pos", 3)
    assert abs(worst.zscore) > OUTLIER_Z
    # good slopes / bad R2 signature: R2 recovers without the outlier
    assert d.r_squared_clean[0] > d.r_squared[0]
    assert d.r_squared_clean[0] > 0.99999
    txt = diagnostics_text(d)
    assert "N1_pos" in txt and "outlier" in txt.lower()


def test_two_outliers_not_masked():
    """Two gross points inflate a single-pass MAD enough to hide each
    other — the iterative rejection must flag BOTH."""
    s = _session()
    for key, idx, dv in (("N1_pos", 3, 0.005), ("N1_neg", 2, -0.003)):
        p = s.points[key][idx]
        p.volts = list(p.volts)
        p.volts[0] += dv
    d = diagnose(s, "Linear")
    flagged = {(p.key, p.index) for p in d.outliers()}
    assert ("N1_pos", 3) in flagged
    assert ("N1_neg", 2) in flagged
    # and the clean preview must actually be clean
    assert d.r_squared_clean[0] > 0.99999
    assert d.r_squared_clean[0] > d.r_squared[0]


def test_exclusion_recovers_fit_and_vol_output():
    s = _session()
    bad = s.points["N1_pos"][3]
    bad.volts = list(bad.volts)
    bad.volts[0] += 0.004
    r2_bad = diagnose(s, "Linear").r_squared[0]
    bad.excluded = True
    d = diagnose(s, "Linear")
    assert d.r_squared[0] > r2_bad
    assert d.r_squared[0] > 0.99999
    # excluded point stays out of the written .vol too
    n_lines_with = vol_text(s).count("\n")
    bad.excluded = False
    assert vol_text(s).count("\n") == n_lines_with + 1


def test_missing_zero_load_section_warned():
    s = _session()
    # strip the zero-load rows from one section
    s.points["Y1_pos"] = [p for p in s.points["Y1_pos"] if p.load != 0]
    d = diagnose(s, "Linear")
    assert any("no 0-load points" in w for w in d.warnings)
    sec = next(x for x in d.sections if x.key == "Y1_pos")
    assert not sec.has_zero


def test_section_offset_suspect_flagged():
    """A whole section shifted by a constant (bad zero) is reported as
    an offset problem, not as individual outliers."""
    s = _session()
    for p in s.points["N2_neg"]:
        p.volts = list(p.volts)
        p.volts[1] += 0.002               # constant bias ≈ 15 lb
    # remove its zero rows so the bias cannot be folded out
    s.points["N2_neg"] = [p for p in s.points["N2_neg"] if p.load != 0]
    d = diagnose(s, "Linear")
    sec = next(x for x in d.sections if x.key == "N2_neg")
    assert sec.offset_suspect
    assert any("offset-shifted" in w for w in d.warnings)


def test_excitation_anomaly_warned():
    s = _session()
    s.points["Ax_pos"][2].excitation = EXC * 0.8      # sagging supply
    d = diagnose(s, "Linear")
    assert any("excitation" in w for w in d.warnings)


def test_channel_anomaly_detected_and_repair_preview():
    """A gross value on a FOREIGN channel (off-diagonal) must be
    caught by the channel scan even though the point's own element
    fits fine — and the repaired preview must recover R^2."""
    s = _session()
    p = s.points["Mx_pos"][3]
    p.volts = list(p.volts)
    p.volts[2] += 0.003                   # Y1 channel glitch in an Mx run
    d = diagnose(s, "Linear")
    hits = [(a.key, a.index, a.channel_name)
            for a in d.channel_anomalies]
    assert ("Mx_pos", 3, "Y1") in hits
    a = next(x for x in d.channel_anomalies
             if (x.key, x.index) == ("Mx_pos", 3))
    assert a.measured_v - a.expected_v == pytest.approx(0.003, rel=0.1)
    assert d.r_squared_repaired is not None
    # Y1's own slope was dragged by the off-diagonal row; repair frees it
    assert d.r_squared_repaired[2] >= d.r_squared[2]
    assert d.r_squared_repaired[2] > 0.99999
    txt = diagnostics_text(d)
    assert "Off-diagonal" in txt and "Mx_pos" in txt


def test_channel_trend_robust_to_the_anomaly():
    from balcal_gui.diagnostics import channel_trend
    s = _session()
    p = s.points["N1_pos"][3]
    p.volts = list(p.volts)
    p.volts[1] += 0.002                   # N2 glitch inside N1 section
    loads, volts, trend, z = channel_trend(s, "N1_pos", 1)
    r = 3                                 # active row of the glitch
    assert abs(z[r]) > 10                 # trend not dragged by it
    assert volts[r] - trend[r] == pytest.approx(0.002, rel=0.05)


def test_crosstalk_burden_note():
    """A section driving a foreign channel harder than its own cal
    sections do must be reported as structural cross-talk."""
    s = _session()
    for p in s.points["Mx_pos"] + s.points["Mx_neg"]:
        p.volts = list(p.volts)
        # Y1 driven at ~75% of its own-section span during Mx runs
        p.volts[2] += 0.06 * p.load / 30.0
    d = diagnose(s, "Linear")
    assert any("drives Y1" in n for n in d.crosstalk_notes)


REAL_VOL = Path(__file__).resolve().parents[1] / "50lbCalV6.vol"


@pytest.mark.skipif(not REAL_VOL.exists(), reason="50lbCalV6 not present")
def test_50lbCalV6_regression():
    """The field case 2026-07-22: linear-looking points, slopes way
    off on Fwd_Pitch / Fwd_Yaw / Mx. Pin what the diagnostics find."""
    from balcal_gui.volfile import read_vol_session
    s = read_vol_session(str(REAL_VOL))
    d = diagnose(s, "Linear")
    ch = d.channels
    # the symptom
    assert d.r_squared[ch.index("Fwd_Pitch")] < 0.5
    assert d.r_squared[ch.index("Mx")] < 0.8
    # the Roll step in Fwd_Yaw_neg is caught as channel anomalies
    assert any(a.key == "Fwd_Yaw_neg" and a.channel_name == "Roll"
               for a in d.channel_anomalies)
    # repairing the flagged values substantially recovers Mx
    assert d.r_squared_repaired is not None
    assert d.r_squared_repaired[ch.index("Mx")] > 0.95
    # and the structural fwd-bridge burden of the Mx sections is named
    assert any("Mx" in n and ("FwdPitch" in n or "FwdYaw" in n)
               for n in d.crosstalk_notes)
    # the slope deficit is measured and attributed to the Mx rows
    fp = ch.index("Fwd_Pitch")
    assert d.own_slopes is not None
    assert d.own_slopes[fp] == pytest.approx(0.371, abs=0.02)
    assert any(n.startswith("Fwd_Pitch") and "[Mx]" in n
               for n in d.influence_notes), d.influence_notes
    # the enabling collinearity of the fwd bridges is called out
    assert any("Fwd_Pitch" in n and "Fwd_Yaw" in n and "collinear" in n
               for n in d.influence_notes)


def test_row_indices_count_excluded_points():
    """PointDiag.index must index the FULL point list (incl. excluded)
    so GUI delete/exclude actions hit the right row."""
    s = _session()
    s.points["N1_pos"][1].excluded = True
    d = diagnose(s, "Linear")
    n1 = [p for p in d.points if p.key == "N1_pos"]
    assert [p.index for p in n1] == [0, 2, 3, 4, 5, 6]
    loads = [s.points["N1_pos"][p.index].load for p in n1]
    assert loads == [p.load for p in n1]
