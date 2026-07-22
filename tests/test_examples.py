"""The shipped example run sheet must load cleanly and demonstrate every
documented import feature.

The PRIMARY example is now the 5-sheet workbook
``examples/Freestream_RunSheet_Template.xlsx`` (loaded via
:mod:`freestream.runbook`); the flat single-sheet ``load_runsheet`` path is
kept working as a simple fallback.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freestream.runbook import (build_run_points, expanded_count,
                                is_runbook_workbook, load_runbook)
from freestream.runsheet import load_runsheet

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"
TEMPLATE = EXAMPLES / "Freestream_RunSheet_Template.xlsx"

# expanded point counts per Run-Matrix row (Mach air-off 0 auto-prepended)
DOC_COUNTS = {
    "run_01": 2, "run_02": 14, "run_03": 6, "run_04": 22,
    "run_05": 44, "run_06": 6, "run_07": 42, "run_08": 18,
}


@pytest.fixture(scope="module")
def book():
    pytest.importorskip("openpyxl")
    assert TEMPLATE.exists(), f"missing shipped template: {TEMPLATE}"
    return load_runbook(TEMPLATE)


def test_template_is_recognized_as_workbook():
    pytest.importorskip("openpyxl")
    assert is_runbook_workbook(TEMPLATE)


def test_five_sheets_parsed(book):
    assert book.friendly_info().get("facility") == \
        "Subsonic Wind Tunnel (SSWT)"
    assert set(book.configs) == {"clean", "flaps10", "hysteresis",
                                 "commissioning"}
    assert set(book.named_arrays) == {"alpha_fine", "alpha_coarse",
                                      "beta_std", "mach_set"}
    assert set(r.run for r in book.runs) == set(DOC_COUNTS)


def test_ref_dims_present(book):
    assert set(book.ref_dims) == {"Sref", "cref", "bref",
                                  "MRC_x", "MRC_y", "MRC_z"}
    # units captured even though the blank template has no numeric values
    assert "in" in str(book.ref_dims["cref"]["units"])


def test_disabled_run_08(book):
    run08 = next(r for r in book.runs if r.run == "run_08")
    assert run08.enable is False
    assert [r.run for r in book.enabled_runs] == [
        "run_01", "run_02", "run_03", "run_04",
        "run_05", "run_06", "run_07"]


def test_documented_point_counts(book):
    for run in book.runs:
        assert expanded_count(book, run) == DOC_COUNTS[run.run], run.run


def test_named_array_and_return_sweep(book):
    # run_07 references @alpha_fine (-4:1:16 → 21 pts) × air-off+0.5
    run07 = next(r for r in book.runs if r.run == "run_07")
    pts = build_run_points(book, run07)
    assert len(pts) == 42
    assert sorted(set(p.mach for p in pts)) == [0, 0.5]
    # run_04 is the hysteresis return sweep with up/dn legs
    run04 = next(r for r in book.runs if r.run == "run_04")
    p4 = build_run_points(book, run04)
    legs = [p.direction for p in p4 if p.mach == 0.35]
    assert legs == ["up"] * 6 + ["dn"] * 5


def test_flat_csv_fallback_still_works(tmp_path):
    import csv
    sheet = tmp_path / "flat.csv"
    with open(sheet, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["alpha", "beta", "mach", "config_name"])
        w.writerow(["0:2:4", "0", "0.3", "clean"])
    assert not is_runbook_workbook(sheet)          # .csv is not a workbook
    pts = load_runsheet(sheet)
    assert [p.alpha for p in pts] == [0.0, 2.0, 4.0]
    assert all(p.mach == 0.3 for p in pts)         # flat path: no air-off
