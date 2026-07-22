"""Round-trip: session → .vol 3.1 → balcal.read_vol_file → fit."""

import sys
from datetime import date
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "devices"))

from balcal_gui.session import BalanceKind, CalSession, TestPoint
from balcal_gui.volfile import vol_text, write_vol
from ni_usb_6351 import balcal

EXAMPLE = Path(__file__).resolve().parents[1] / "2025_06_06_2 100 lb.vol"

SENS = [0.000134, 0.000135, 0.000270, 0.000268, 0.000560, 0.000265]
EXCITATION = 9.8633


def _make_session(kind=BalanceKind.FORCE) -> CalSession:
    """Synthetic ideal balance: each element responds linearly to its own
    load with sensitivity SENS[i] V/unit (times excitation-normalized),
    zero cross-talk, small fixed offsets."""
    s = CalSession(kind=kind, operator="pytest",
                   cal_date=date(2026, 7, 17), serial_number="TEST-1",
                   outer_diameter="0.75 in.")
    for i, el in enumerate(s.elements):
        s.max_loads[el.name] = 100.0
    s.distances.update({"x1": 1.5, "x2": 1.5, "y1": 1.25, "y2": 1.25})
    offsets = [1e-4 * (i + 1) for i in range(6)]
    for i, el in enumerate(s.elements):
        for positive in (True, False):
            key = f"{el.name}_{'pos' if positive else 'neg'}"
            sign = 1 if positive else -1
            loads = [0, 10, 20, 30, 20, 10, 0]
            for load in loads:
                signed = sign * load
                volts = list(offsets)
                volts[i] += SENS[i] * signed * EXCITATION
                s.add_point(key, TestPoint(load=signed, volts=volts,
                                           excitation=EXCITATION))
    return s


def test_writer_format_header(tmp_path):
    s = _make_session()
    text = vol_text(s)
    lines = text.splitlines()
    assert lines[0] == "Voltage Calibration File 3.1"
    assert lines[1] == "Date--> 7/17/2026"
    assert lines[2] == "Calibration performed by--> pytest"
    assert "[Balance Description]" in lines
    assert "Balance Type--> 5 Force/1 Moment" in lines
    assert "N1--> 100 lb" in lines
    assert "Mx--> 100 in-lb" in lines
    assert ("Distance from N1 to center of Balance x1 in inches--> 1.5"
            in lines)
    assert "[N1 pos]" in lines
    assert "Number of Loads--> 7" in lines
    hdr = ("Load[lb], N1[Volt], N2[Volt], Y1[Volt], Y2[Volt], Ax[Volt], "
           "Roll[Volt], Exct.[Volt]")
    assert hdr in lines
    # roll section uses in-lb loads
    i = lines.index("[Mx pos]")
    assert lines[i + 2].startswith("Load[in-lb],")


def test_round_trip_parses_and_fits(tmp_path):
    s = _make_session()
    path = tmp_path / "roundtrip.vol"
    write_vol(s, str(path))

    cal = balcal.read_vol_file(str(path))
    assert cal.description.balance_type == "5 Force/1 Moment"
    assert cal.description.serial_number == "TEST-1"
    assert cal.force_channels == ["N1", "N2", "Y1", "Y2", "Ax", "Mx"]
    assert cal.max_loads.values["N1"] == 100.0
    assert cal.max_loads.units["Mx"] == "in-lb"
    assert cal.distances.values  # x1..y2 parsed
    assert cal.force.shape == (6 * 2 * 7, 6)

    cal = balcal.calc_coeffs(cal, "Linear")
    assert np.all(cal.r_squared > 0.999999)
    # recovered sensitivity: force = volts_norm / SENS on the diagonal
    for i in range(6):
        assert cal.coeffs[i, i] == pytest.approx(1.0 / SENS[i], rel=1e-6)


def test_negative_sections_signed_loads(tmp_path):
    s = _make_session()
    path = tmp_path / "neg.vol"
    write_vol(s, str(path))
    cal = balcal.read_vol_file(str(path))
    # parser folds |load| * section multiplier; neg sections → negative
    n1_rows = cal.force[:14, 0]
    assert n1_rows.max() == 30.0 and n1_rows.min() == -30.0


def test_moment_balance_sections(tmp_path):
    s = _make_session(BalanceKind.MOMENT)
    path = tmp_path / "mb.vol"
    write_vol(s, str(path))
    text = path.read_text()
    assert "Balance Type--> 1 Force/5 Moment" in text
    assert "[Aft_Pitch pos]" in text
    assert ("Distance from Aft_Pitch to center of Balance x1 in "
            "inches--> 1.5" in text)
    cal = balcal.read_vol_file(str(path))
    assert cal.force_channels == ["Aft_Pitch", "Aft_Yaw", "Fwd_Pitch",
                                  "Fwd_Yaw", "Ax", "Mx"]


def test_example_file_still_parses():
    """Reference: the shipped example must parse with the same reader we
    round-trip against."""
    cal = balcal.read_vol_file(str(EXAMPLE))
    assert cal.force_channels == ["N1", "N2", "Y1", "Y2", "Ax", "Mx"]
    cal = balcal.calc_coeffs(cal, "Linear")
    assert np.all(cal.r_squared > 0.99)


def test_missing_max_load_omits_line_and_still_parses(tmp_path):
    """A blank Max Load cell must never produce an unparseable file."""
    s = _make_session()
    del s.max_loads["N2"]
    text = vol_text(s)
    assert "N2-->" not in text
    path = tmp_path / "nomax.vol"
    write_vol(s, str(path))
    cal = balcal.read_vol_file(str(path))       # must not raise
    assert "N2" not in cal.max_loads.values
    assert cal.max_loads.values["N1"] == 100.0


def test_missing_distance_omitted_not_zero():
    s = _make_session()
    del s.distances["x2"]
    text = vol_text(s)
    assert "Distance from N2" not in text
    assert "inches--> 0" not in text


def test_validate_session_reports_gaps():
    from balcal_gui.volfile import validate_session
    s = _make_session()
    assert validate_session(s) == []
    del s.max_loads["Ax"]
    del s.distances["y1"]
    s.operator = ""
    warnings = " | ".join(validate_session(s))
    assert "Ax" in warnings and "Y1" in warnings
    assert "Operator" in warnings


def test_non_ascii_never_truncates_existing_file(tmp_path):
    good = _make_session()
    path = tmp_path / "keep.vol"
    write_vol(good, str(path))
    original = path.read_bytes()
    bad = _make_session()
    bad.operator = "José García"
    with pytest.raises(UnicodeEncodeError):
        write_vol(bad, str(path))
    assert path.read_bytes() == original        # untouched, not 0 bytes


def test_fractional_load_precision(tmp_path):
    """Moment-arm products must survive the round trip (no %g rounding)."""
    s = _make_session()
    load = 116.1476418
    s.add_point("N1_pos", TestPoint(load=load,
                                    volts=[1e-4] * 6,
                                    excitation=EXCITATION))
    path = tmp_path / "frac.vol"
    write_vol(s, str(path))
    cal = balcal.read_vol_file(str(path))
    assert np.any(np.isclose(cal.force[:, 0], load, atol=1e-6))


@pytest.mark.filterwarnings("ignore::RuntimeWarning")  # single-channel
def test_per_row_excitation_normalization(tmp_path):
    """The parser divides each row by that row's excitation — pin it."""
    s = CalSession(kind=BalanceKind.FORCE, operator="pytest",
                   cal_date=date(2026, 7, 17), serial_number="EXC-1",
                   outer_diameter="0.75 in.")
    for el in s.elements:
        s.max_loads[el.name] = 100.0
    s.distances.update({"x1": 1.5, "x2": 1.5, "y1": 1.25, "y2": 1.25})
    sens = 1.3e-4                                # V per lb per V-exc
    loads = [0, 10, 20, 30, 20, 10, 0]
    for i, load in enumerate(loads):
        exc = 9.8 + 0.05 * i                     # drifting supply
        volts = [0.0] * 6
        volts[0] = sens * load * exc
        s.add_point("N1_pos", TestPoint(load=load, volts=volts,
                                        excitation=exc))
    path = tmp_path / "exc.vol"
    write_vol(s, str(path))
    cal = balcal.read_vol_file(str(path))
    cal = balcal.calc_coeffs(cal, "Linear")
    # normalization by per-row excitation makes the fit exact despite
    # the drifting supply
    assert cal.r_squared[0] > 0.999999
    assert cal.coeffs[0, 0] == pytest.approx(1.0 / sens, rel=1e-6)


def test_read_vol_session_round_trip(tmp_path):
    """write → read_vol_session → write must be lossless (metadata,
    points, order, values) so calibrations can be edited/appended."""
    from balcal_gui.volfile import read_vol_session
    s = _make_session()
    s.distances["roll_arm"] = 2.5      # not stored in the format
    path = tmp_path / "rt.vol"
    write_vol(s, str(path))

    r = read_vol_session(str(path))
    assert r.kind is BalanceKind.FORCE
    assert r.operator == "pytest"
    assert r.cal_date == date(2026, 7, 17)
    assert r.serial_number == "TEST-1"
    assert r.outer_diameter == "0.75 in."
    assert r.max_loads == {el.name: 100.0 for el in s.elements}
    assert r.distances == {"x1": 1.5, "x2": 1.5,
                           "y1": 1.25, "y2": 1.25}
    assert set(r.points) == set(s.points)
    for key in s.points:
        assert [p.load for p in r.points[key]] == \
            [p.load for p in s.points[key]]
        for rp, sp in zip(r.points[key], s.points[key]):
            assert rp.volts == pytest.approx(sp.volts, abs=1e-14)
            assert rp.excitation == pytest.approx(sp.excitation)
            assert rp.stds is None     # stds are not persisted

    # second write must be byte-identical (true lossless round trip)
    path2 = tmp_path / "rt2.vol"
    write_vol(r, str(path2))
    assert path2.read_bytes() == path.read_bytes()


def test_read_vol_session_moment_balance(tmp_path):
    from balcal_gui.volfile import read_vol_session
    s = _make_session(BalanceKind.MOMENT)
    path = tmp_path / "mb_rt.vol"
    write_vol(s, str(path))
    r = read_vol_session(str(path))
    assert r.kind is BalanceKind.MOMENT
    assert "Aft_Pitch_pos" in r.points
    assert r.point_count() == s.point_count()


def test_read_vol_session_rejects_tab_delimited():
    """The old MATLAB V1 files are tab-delimited — clear error, not
    silent garbage."""
    from balcal_gui.volfile import read_vol_session
    v1 = (Path(__file__).resolve().parents[1] / "FB_Cal_GUI" /
          "ForceCal_1" / "for_redistribution_files_only" / "cal.vol")
    if not v1.exists():
        pytest.skip("V1 example not present")
    with pytest.raises(ValueError):
        read_vol_session(str(v1))


def test_number_formats_match_example():
    s = _make_session()
    text = vol_text(s)
    volts = [f for f in text.splitlines() if f.startswith("0, ")]
    assert volts, "expected zero-load data rows"
    first = volts[0].split(", ")
    assert first[0] == "0"
    for v in first[1:]:
        mantissa, exp = v.split("E")
        assert len(mantissa.split(".")[1]) == 10
        assert exp[0] in "+-" and len(exp) == 3
