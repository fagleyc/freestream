"""Tests for freestream.runsheet — axis specs, grid expansion, run-sheet import."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest

from freestream.runsheet import (SweepPoint, build_grid, load_runsheet,
                                parse_axis_spec, points_summary)


# ---------------------------------------------------------------------------
# parse_axis_spec
# ---------------------------------------------------------------------------

class TestParseAxisSpec:
    """The NEW canonical grammar: ``start:delta:end`` (the MIDDLE value is
    the step/delta, end inclusive) — see freestream.sweepgrammar."""

    def test_comma_list(self):
        assert parse_axis_spec("0,2,4") == [0.0, 2.0, 4.0]

    def test_comma_list_with_spaces_and_negatives(self):
        assert parse_axis_spec(" -4, 0 , 3.5 ") == [-4.0, 0.0, 3.5]

    def test_range_start_delta_end(self):
        assert parse_axis_spec("-4:2:8") == [
            -4.0, -2.0, 0.0, 2.0, 4.0, 6.0, 8.0]
        assert parse_axis_spec("0:2:10") == [0.0, 2.0, 4.0, 6.0, 8.0, 10.0]

    def test_descending_range(self):
        # negative delta (or end < start) walks downward
        assert parse_axis_spec("10:-2:0") == [10.0, 8.0, 6.0, 4.0, 2.0, 0.0]

    def test_float_delta(self):
        assert parse_axis_spec("0.2:0.05:0.4") == pytest.approx(
            [0.2, 0.25, 0.3, 0.35, 0.4])

    def test_return_sweep(self):
        assert parse_axis_spec("0:2:10R") == [
            0.0, 2.0, 4.0, 6.0, 8.0, 10.0, 8.0, 6.0, 4.0, 2.0, 0.0]

    def test_partial_last_point_dropped(self):
        # end not hit exactly by the delta → drop the partial tail
        assert parse_axis_spec("0:2:9") == [0.0, 2.0, 4.0, 6.0, 8.0]

    def test_single_number(self):
        assert parse_axis_spec("5") == [5.0]
        assert parse_axis_spec("-2.5") == [-2.5]
        assert parse_axis_spec(3) == [3.0]
        assert parse_axis_spec(1.5) == [1.5]

    def test_named_reference(self):
        named = {"alpha_fine": "-4:2:0"}
        assert parse_axis_spec("@alpha_fine", named=named) == [
            -4.0, -2.0, 0.0]

    def test_invalid_specs_raise(self):
        # blank/garbage/unknown-named/two-part ranges are rejected
        for bad in ("", "  ", "abc", "R", "0:3", "1:2:3:4", "@missing"):
            with pytest.raises(ValueError):
                parse_axis_spec(bad)


# ---------------------------------------------------------------------------
# build_grid
# ---------------------------------------------------------------------------

class TestBuildGrid:
    def test_default_order_mach_outer_alpha_inner(self):
        pts = build_grid(alpha_spec=[0.0, 2.0], mach_spec=[0.2, 0.4])
        assert len(pts) == 4
        assert [(p.mach, p.alpha) for p in pts] == [
            (0.2, 0.0), (0.2, 2.0), (0.4, 0.0), (0.4, 2.0)]
        assert all(p.beta is None for p in pts)

    def test_custom_order_alpha_outer(self):
        pts = build_grid(alpha_spec=[0.0, 2.0], mach_spec=[0.2, 0.4],
                         order=("alpha", "beta", "mach"))
        assert [(p.alpha, p.mach) for p in pts] == [
            (0.0, 0.2), (0.0, 0.4), (2.0, 0.2), (2.0, 0.4)]

    def test_counts_three_axes(self):
        pts = build_grid(alpha_spec="0:2:4", beta_spec="0,5",
                         mach_spec=[0.2, 0.3, 0.4])
        assert len(pts) == 3 * 2 * 3

    def test_mach_spec_string_expansion(self):
        pts = build_grid(mach_spec="0.2:0.1:0.4")
        assert [p.mach for p in pts] == [
            pytest.approx(0.2), pytest.approx(0.3), pytest.approx(0.4)]

    def test_mach_return_sweep_spec(self):
        pts = build_grid(mach_spec="0.2:0.1:0.4R")
        assert [p.mach for p in pts] == [
            pytest.approx(m) for m in (0.2, 0.3, 0.4, 0.3, 0.2)]

    def test_string_specs_parsed(self):
        pts = build_grid(alpha_spec="0:2:4")
        assert [p.alpha for p in pts] == [0.0, 2.0, 4.0]

    def test_none_specs_single_point(self):
        pts = build_grid()
        assert len(pts) == 1
        assert (pts[0].alpha, pts[0].beta, pts[0].mach) == (None, None, None)

    def test_stamped_fields_and_meta_copies(self):
        meta = {"flap_deg": 10}
        pts = build_grid(alpha_spec=[0.0, 2.0], dwell_s=1.5, samples=250,
                         air_state="AirOff", meta=meta, row_index=7)
        assert all(p.dwell_s == 1.5 and p.samples == 250 for p in pts)
        assert all(p.air_state == "AirOff" and p.row_index == 7 for p in pts)
        assert all(p.meta == {"flap_deg": 10} for p in pts)
        # each point owns its meta dict
        pts[0].meta["x"] = 1
        assert "x" not in pts[1].meta and "x" not in meta

    def test_defaults(self):
        p = build_grid(alpha_spec=0)[0]
        assert p.air_state == "AirOn"
        assert p.status == "queued"

    def test_unknown_axis_in_order_raises(self):
        # "rpm" is no longer an axis — it is a meta-level override column
        with pytest.raises(ValueError):
            build_grid(alpha_spec=[0.0], order=("rpm", "alpha"))


# ---------------------------------------------------------------------------
# load_runsheet — CSV
# ---------------------------------------------------------------------------

def _write_csv(path, headers, rows):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(headers)
        writer.writerows(rows)


class TestLoadRunsheetCsv:
    def test_expansion_and_meta_inheritance(self, tmp_path):
        sheet = tmp_path / "runsheet.csv"
        _write_csv(
            sheet,
            ["Alpha", "Beta", "Mach", "Dwell", "Samples", "Air_State",
             "flap_deg", "config_name", "Notes"],
            [
                ["0:2:4", "0", "0.3", "1.0", "200", "AirOn",
                 "10", "clean", "first sweep"],
                ["-2", "", "0.2,0.4", "", "", "AirOff",
                 "25", "flaps25", ""],
            ],
        )
        pts = load_runsheet(sheet)
        # row 0: alpha 0,2,4 x beta 0 x mach 0.3 = 3; row 1: 1 x mach 2 = 2
        assert len(pts) == 5

        row0 = [p for p in pts if p.row_index == 0]
        assert [p.alpha for p in row0] == [0.0, 2.0, 4.0]
        assert all(p.beta == 0.0 and p.mach == 0.3 for p in row0)
        assert all(p.dwell_s == 1.0 and p.samples == 200 for p in row0)
        assert all(p.air_state == "AirOn" for p in row0)
        # unknown columns inherited verbatim (CSV → strings), original headers
        assert all(p.meta == {"flap_deg": "10", "config_name": "clean",
                              "Notes": "first sweep"} for p in row0)

        row1 = [p for p in pts if p.row_index == 1]
        assert [(p.mach, p.alpha) for p in row1] == [(0.2, -2.0),
                                                     (0.4, -2.0)]
        assert all(p.beta is None for p in row1)
        assert all(p.air_state == "AirOff" for p in row1)
        assert all(p.meta["flap_deg"] == "25" and
                   p.meta["config_name"] == "flaps25" for p in row1)

    def test_rpm_column_is_direct_override_in_meta(self, tmp_path):
        # "rpm" is NOT an axis: it is a documented direct-RPM override
        # stored (as float) in meta so the sweep bypasses the Mach loop.
        sheet = tmp_path / "override.csv"
        _write_csv(sheet, ["alpha", "mach", "rpm", "notes"],
                   [["0,2", "", "600", "commissioning"],
                    ["1", "0.3", "", "normal mach point"]])
        pts = load_runsheet(sheet)
        row0 = [p for p in pts if p.row_index == 0]
        assert len(row0) == 2
        assert all(p.mach is None for p in row0)
        assert all(p.meta["rpm"] == 600.0 for p in row0)
        assert all(isinstance(p.meta["rpm"], float) for p in row0)
        (p1,) = [p for p in pts if p.row_index == 1]
        assert p1.mach == 0.3 and "rpm" not in p1.meta

    def test_air_state_defaults_airon_when_column_missing(self, tmp_path):
        sheet = tmp_path / "noair.csv"
        _write_csv(sheet, ["alpha", "operator"], [["1,2", "casey"]])
        pts = load_runsheet(sheet)
        assert len(pts) == 2
        assert all(p.air_state == "AirOn" for p in pts)
        assert all(p.meta == {"operator": "casey"} for p in pts)

    def test_unknown_columns_never_error_and_preserved(self, tmp_path):
        sheet = tmp_path / "weird.csv"
        _write_csv(sheet,
                   ["alpha", "gear", "Totally Custom Column!", "hysteresis_id"],
                   [["0", "down", "anything at all", "H-7"]])
        pts = load_runsheet(sheet)
        assert len(pts) == 1
        assert pts[0].meta == {"gear": "down",
                               "Totally Custom Column!": "anything at all",
                               "hysteresis_id": "H-7"}

    def test_blank_rows_skipped_and_return_sweep_cell(self, tmp_path):
        sheet = tmp_path / "blanks.csv"
        _write_csv(sheet, ["alpha", "config_name"],
                   [["0:2:4R", "hyst"], ["", ""], ["1", "solo"]])
        pts = load_runsheet(sheet)
        assert [p.alpha for p in pts if p.row_index == 0] == [
            0.0, 2.0, 4.0, 2.0, 0.0]
        assert [p.alpha for p in pts if p.row_index == 2] == [1.0]
        assert len(pts) == 6

    def test_unsupported_extension_raises(self, tmp_path):
        bad = tmp_path / "sheet.txt"
        bad.write_text("alpha\n1\n")
        with pytest.raises(ValueError):
            load_runsheet(bad)


# ---------------------------------------------------------------------------
# load_runsheet — XLSX (openpyxl)
# ---------------------------------------------------------------------------

class TestLoadRunsheetXlsx:
    def test_expansion_and_native_meta_types(self, tmp_path):
        openpyxl = pytest.importorskip("openpyxl")
        sheet = tmp_path / "runsheet.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.append(["alpha", "beta", "mach", "dwell_s", "samples",
                   "air_state", "flap_deg", "config_name"])
        ws.append(["0:2:4", 0, 0.3, 0.75, 150, "AirOn", 10, "clean"])
        ws.append([-2, 1.5, None, None, None, None, 25, "flaps25"])
        wb.save(sheet)

        pts = load_runsheet(sheet)
        assert len(pts) == 4

        row0 = [p for p in pts if p.row_index == 0]
        assert [p.alpha for p in row0] == [0.0, 2.0, 4.0]
        assert all(p.beta == 0.0 and p.mach == 0.3 for p in row0)
        assert all(p.dwell_s == 0.75 and p.samples == 150 for p in row0)
        # xlsx meta values arrive verbatim with native types
        assert all(p.meta == {"flap_deg": 10, "config_name": "clean"}
                   for p in row0)

        (p1,) = [p for p in pts if p.row_index == 1]
        assert (p1.alpha, p1.beta, p1.mach) == (-2.0, 1.5, None)
        assert p1.air_state == "AirOn"  # blank cell → default
        assert p1.meta == {"flap_deg": 25, "config_name": "flaps25"}


# ---------------------------------------------------------------------------
# points_summary
# ---------------------------------------------------------------------------

class TestPointsSummary:
    def test_empty(self):
        assert points_summary([]) == "0 points"

    def test_full_summary(self):
        pts = []
        for row in range(3):
            pts.extend(build_grid(alpha_spec="-4:6:8",
                                  mach_spec=[0.2, 0.4],
                                  row_index=row))
        summary = points_summary(pts)
        assert summary.startswith("18 points:")
        assert "alpha -4..8" in summary
        assert "2 mach levels" in summary
        assert "3 rows" in summary

    def test_single_values(self):
        pts = [SweepPoint(alpha=2.0, mach=0.3, row_index=0)]
        summary = points_summary(pts)
        assert "1 point:" in summary
        assert "alpha 2" in summary
        assert "mach 0.3" in summary
        assert "1 row" in summary
