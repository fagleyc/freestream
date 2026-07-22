"""Offscreen GUI tests: the run-sheet import dialog + the planner indicator."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication            # noqa: E402

from freestream.config import FreestreamConfig        # noqa: E402
from freestream.runbook import load_runbook           # noqa: E402
from freestream.app.planner import PlannerPanel       # noqa: E402
from freestream.app.runsheet_dialog import RunSheetDialog  # noqa: E402

from test_runbook import (DEFAULT_CONFIGS, DEFAULT_NAMED, DEFAULT_RUNS,  # noqa: E402
                          make_workbook)

pytest.importorskip("openpyxl")


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


@pytest.fixture
def book(tmp_path):
    path = make_workbook(
        tmp_path / "rs.xlsx", runs=DEFAULT_RUNS, configs=DEFAULT_CONFIGS,
        named=DEFAULT_NAMED,
        ref={"Sref": 2.5, "cref": 0.5, "bref": 5.0,
             "MRC_x": 1.0, "MRC_y": 0.0, "MRC_z": 0.25},
        info={"test_name": "T-1", "model_name": "NACA0012",
              "engineer": "Casey", "operator": "cadet"})
    return load_runbook(path)


def test_dialog_selects_run_and_returns_matrix(app, book):
    dlg = RunSheetDialog(book)
    app.processEvents()
    # matrix shows every run row with the live expanded-point count
    assert dlg.matrix.rowCount() == 2
    assert dlg.matrix.item(0, 5).text() == "44"     # run_a # pts column
    # select run_a and accept
    dlg.matrix.selectRow(0)
    app.processEvents()
    dlg._accept_selected()
    run_row, points = dlg.result_points()
    assert run_row.run == "run_a"
    assert len(points) == 44
    assert sorted(set(p.mach for p in points)) == [0, 0.3, 0.5, 0.7]
    dlg.deleteLater()


def test_dialog_all_enabled(app, book):
    dlg = RunSheetDialog(book)
    dlg._accept_all_enabled()
    run_row, points = dlg.result_points()
    assert run_row is None
    assert len(points) == 44            # only run_a is enabled (run_b is N)
    dlg.deleteLater()


def test_planner_indicator_reflects_loaded_run(app, book):
    cfg = FreestreamConfig(samples=100, sample_rate_hz=200.0)
    planner = PlannerPanel(cfg)
    app.processEvents()
    # nothing loaded → dim placeholder
    assert "no run sheet loaded" in planner.indicator.text()

    run = book.runs[0]                  # run_a
    from freestream.runbook import build_run_points
    points = build_run_points(book, run)
    planner.apply_run_selection(book, run, points)
    app.processEvents()

    text = planner.indicator.text()
    assert "run_a" in text
    assert "config: clean" in text
    assert "M[0, 0.3, 0.5, 0.7]" in text
    assert "44 pts" in text
    # axis edits were filled with the run's cells
    assert planner.alpha_edit.text() == "-4:2:16"
    assert planner.mach_edit.text() == "0.3,0.5,0.7"
    # the run's acquisition + reference dims folded into the config
    assert cfg.samples == 1000
    assert cfg.sample_rate_hz == 2000
    assert cfg.Sref == 2.5
    assert cfg.model_name == "NACA0012"
    assert len(planner.points) == 44

    # live update: shrink alpha → point count + summary follow
    planner.alpha_edit.setText("0,2")
    app.processEvents()
    assert "8 pts" in planner.indicator.text()   # 2 alpha × 4 mach
    planner.shutdown()
    planner.deleteLater()
