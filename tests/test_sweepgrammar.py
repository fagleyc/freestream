"""Tests for freestream.sweepgrammar — the ONE canonical sweep-cell grammar."""

from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freestream import sweepgrammar as g


class TestExpand:
    def test_blank(self):
        assert g.expand("") == []
        assert g.expand(None) == []
        assert g.expand("none") == []

    def test_single(self):
        assert g.expand("2") == [2]
        assert g.expand("-2.5") == [-2.5]

    def test_comma_list(self):
        assert g.expand("0,2,4,6") == [0, 2, 4, 6]

    def test_range_start_delta_end(self):
        assert g.expand("-4:2:8") == [-4, -2, 0, 2, 4, 6, 8]

    def test_descending_range(self):
        assert g.expand("10:-2:0") == [10, 8, 6, 4, 2, 0]
        assert g.expand("4:2:-4") == [4, 2, 0, -2, -4]

    def test_partial_last_point_dropped(self):
        assert g.expand("0:2:9") == [0, 2, 4, 6, 8]

    def test_float_delta(self):
        assert g.expand("0.2:0.05:0.4") == pytest.approx(
            [0.2, 0.25, 0.3, 0.35, 0.4])

    def test_mix_range_plus_points(self):
        assert g.expand("-4:2:8, 10, 12") == [
            -4, -2, 0, 2, 4, 6, 8, 10, 12]

    def test_return_sweep_values(self):
        assert g.expand("0:2:10R") == [
            0, 2, 4, 6, 8, 10, 8, 6, 4, 2, 0]

    def test_zero_delta_is_single_point(self):
        assert g.expand("5:0:5") == [5]


class TestReturnLegTags:
    def test_up_dn_tags(self):
        tagged = g.expand_tagged("0:2:4R")
        assert tagged == [(0, "up"), (2, "up"), (4, "up"),
                          (2, "dn"), (0, "dn")]

    def test_monotonic_has_no_tag(self):
        assert all(leg is None for _, leg in g.expand_tagged("0:2:8"))

    def test_apex_not_duplicated(self):
        vals = g.expand("0:5:10R")
        assert vals == [0, 5, 10, 5, 0]        # single apex 10


class TestNamed:
    def test_named_reference(self):
        named = {"alpha_fine": "-4:2:4", "beta_std": "-4,0,4"}
        assert g.expand("@alpha_fine", named) == [-4, -2, 0, 2, 4]
        assert g.expand("@beta_std", named) == [-4, 0, 4]

    def test_named_within_mix(self):
        named = {"a": "0,2"}
        assert g.expand("@a, 4, 6", named) == [0, 2, 4, 6]

    def test_unknown_named_raises(self):
        with pytest.raises(g.GrammarError):
            g.expand("@nope", {})


class TestCsv:
    def test_csv_column_via_loader(self, tmp_path):
        path = tmp_path / "aoa.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            for v in (-2, 0, 3, 5):
                w.writerow([v])

        def loader(ref):
            with open(tmp_path / ref, newline="", encoding="utf-8") as fh:
                return [float(row[0]) for row in csv.reader(fh) if row]

        assert g.expand("csv:aoa.csv", csv_loader=loader) == [-2, 0, 3, 5]

    def test_csv_without_loader_raises(self):
        with pytest.raises(g.GrammarError):
            g.expand("csv:missing.csv")


class TestMachAirOff:
    def test_prepends_zero(self):
        assert g.expand("0.3", ensure_zero_for_mach=True) == [0, 0.3]
        assert g.expand("0.3,0.5,0.7", ensure_zero_for_mach=True) == [
            0, 0.3, 0.5, 0.7]

    def test_no_double_zero(self):
        assert g.expand("0", ensure_zero_for_mach=True) == [0]
        assert g.expand("0,0.4", ensure_zero_for_mach=True) == [0, 0.4]

    def test_blank_stays_empty(self):
        assert g.expand("", ensure_zero_for_mach=True) == []


class TestBuildPoints:
    def test_mach_outer_air_off_first_alpha_inner(self):
        pts = g.build_points("-4:4:4", "0", "0.5")
        # Mach outer (air-off 0 first), alpha innermost
        assert [(p["mach"], p["beta"], p["alpha"]) for p in pts] == [
            (0, 0, -4), (0, 0, 0), (0, 0, 4),
            (0.5, 0, -4), (0.5, 0, 0), (0.5, 0, 4)]

    def test_count_matches_product(self):
        pts = g.build_points("-4:2:16", "0", "0.3,0.5,0.7")
        assert len(pts) == 11 * 1 * 4          # air-off adds one Mach level

    def test_blank_axes_single_placeholder(self):
        pts = g.build_points("", "", "")
        assert pts == [{"mach": 0, "beta": None, "alpha": None,
                        "leg": None}]

    def test_leg_rides_on_innermost(self):
        pts = g.build_points("0:2:4R", "0", "0.3")
        legs = [p["leg"] for p in pts]
        # 2 Mach levels (air-off 0 + 0.3), each carrying the alpha return
        # sweep's up*3 + dn*2 legs
        assert legs.count("up") == 6 and legs.count("dn") == 4
        assert all(leg in ("up", "dn") for leg in legs)
