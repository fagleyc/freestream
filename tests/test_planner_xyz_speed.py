"""Sweep-planner speed row in the traverse (xyz) axis mode.

The xyz axis set now carries the speed row too, so a traverse flow
survey can step the tunnel speed (nested OUTERMOST, exactly like aero).
Everything the aero speed row does — configured entry unit, canonical
Mach conversion, entered-value meta stamping, the rpm direct path —
behaves identically, with ONE deliberate difference: the air-off 0 is
NOT auto-prepended. The air-off pass exists for balance weight tares as
the model attitude changes; a traverse survey moves a probe, and with
speed outermost a forced 0 would prepend the ENTIRE spatial matrix at
air-off. The operator types 0 explicitly when a tare pass is wanted.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "devices"))

from PyQt6.QtWidgets import QApplication              # noqa: E402

from freestream import speed                          # noqa: E402
from freestream.config import FreestreamConfig        # noqa: E402
from freestream.app.planner import PlannerPanel, _AXIS_SETS  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


def _xyz_planner(**cfg_kw):
    planner = PlannerPanel(FreestreamConfig(**cfg_kw))
    planner.set_axis_mode("xyz")
    return planner


def _headers(planner):
    return [planner.table.horizontalHeaderItem(i).text()
            for i in range(planner.table.columnCount())]


# ── the xyz set carries the speed row ────────────────────────────────────
def test_xyz_set_has_speed_row_and_table_column(app):
    assert [f[0] for f in _AXIS_SETS["xyz"]] == ["x", "y", "z", "mach"]
    planner = _xyz_planner()
    assert "mach" in planner._axis_edits
    assert planner._axis_labels["mach"].text() == "speed [Mach]"
    # the point table grew the speed column (aero stays at 5)
    assert planner.table.columnCount() == 6
    assert _headers(planner) == ["#", "x", "y", "z", "mach", "status"]


def test_axis_mode_round_trip_restores_aero_columns(app):
    planner = _xyz_planner()
    planner.set_axis_mode("aero")
    assert planner.table.columnCount() == 5
    assert _headers(planner) == ["#", "alpha", "beta", "mach", "status"]
    assert planner._axis_labels["mach"].text() == "speed [Mach]"


def test_xyz_placeholder_never_promises_the_auto_zero(app):
    """The planner hints speak the truth: aero keeps the "(air-off 0
    added)" note; the xyz speed row must NOT promise a point Build Grid
    won't produce — in every unit."""
    planner = _xyz_planner()
    for unit in speed.SPEED_UNITS:
        planner.set_speed_unit(unit)
        hint = planner._axis_edits["mach"].placeholderText()
        assert hint
        assert "air-off 0 added" not in hint, unit
    aero = PlannerPanel(FreestreamConfig())
    for unit in speed.SPEED_UNITS:
        aero.set_speed_unit(unit)
        assert "air-off 0 added" in \
            aero._axis_edits["mach"].placeholderText(), unit


# ── building xyz + speed grids ───────────────────────────────────────────
def test_build_xyz_speed_grid_nests_speed_outermost_no_auto_zero(app):
    planner = _xyz_planner()
    planner._axis_edits["x"].setText("0:1:2")
    planner._axis_edits["z"].setText("1")
    planner._axis_edits["mach"].setText("0.05,0.1")
    planner._build_clicked()
    pts = planner.points
    # 2 speeds × 3 x positions — and NO air-off 0 was prepended
    assert len(pts) == 6
    assert all(p.mach is not None and abs(p.mach) > 1e-9 for p in pts)
    # speed nests OUTERMOST (runsheet.DEFAULT_ORDER); x varies fastest
    assert [(p.mach, p.x) for p in pts] == [
        (0.05, 0.0), (0.05, 1.0), (0.05, 2.0),
        (0.1, 0.0), (0.1, 1.0), (0.1, 2.0)]
    assert all(p.z == 1.0 and p.y is None for p in pts)
    # mach entry unit: canonical axis only — no speed meta, no rpm
    assert all("speed_value" not in p.meta for p in pts)
    assert all("rpm" not in p.meta for p in pts)
    # the table mirrors the grid: 6 rows × 6 cols, speed cells rendered
    assert planner.table.rowCount() == 6
    assert planner.table.item(0, 4).text() == "0.05"
    assert planner.table.item(3, 4).text() == "0.1"


def test_build_xyz_velocity_unit_converts_and_stamps_meta(app):
    """Unit conversion + entered-value stamping are IDENTICAL to aero —
    only the auto-zero differs."""
    planner = _xyz_planner(speed_unit="ft/s", speed_tolerance=2.0)
    assert planner._axis_labels["mach"].text() == "speed [ft/s]"
    planner._axis_edits["x"].setText("0,2")
    planner._axis_edits["mach"].setText("100")
    planner._build_clicked()
    pts = planner.points
    assert len(pts) == 2                     # no air-off 0 in ft/s either
    for p in pts:
        assert p.mach == pytest.approx(100.0 * 0.3048 / speed.A0_MS)
        assert p.meta["speed_value"] == pytest.approx(100.0)
        assert p.meta["speed_unit"] == "ft/s"
        assert "rpm" not in p.meta
    # the speed column header names the entered unit; cells show the
    # value the operator typed
    assert _headers(planner)[4] == "speed [ft/s]"
    assert planner.table.item(0, 4).text() == "100"


def test_build_xyz_rpm_unit_routes_direct_rpm_path(app):
    planner = _xyz_planner(speed_unit="rpm", rpm_per_mach=1500.0)
    planner._axis_edits["x"].setText("0")
    planner._axis_edits["mach"].setText("300,600")
    planner._build_clicked()
    pts = planner.points
    assert len(pts) == 2                     # no auto 0 RPM point
    assert [p.meta.get("rpm") for p in pts] == [300.0, 600.0]
    assert [p.meta.get("speed_value") for p in pts] == [300.0, 600.0]
    assert pts[0].mach == pytest.approx(0.2)
    assert pts[1].mach == pytest.approx(0.4)


def test_xyz_explicit_zero_still_builds_a_tare_pass(app):
    """The operator can still ask for the air-off pass — by typing the
    0 explicitly (nothing filters it out)."""
    planner = _xyz_planner()
    planner._axis_edits["x"].setText("0,1")
    planner._axis_edits["mach"].setText("0,0.05")
    planner._build_clicked()
    assert [p.mach for p in planner.points] == [0.0, 0.0, 0.05, 0.05]


def test_xyz_blank_speed_builds_a_pure_position_matrix(app):
    """A blank speed row omits the tunnel axis entirely (mach None on
    every point) — the historical Mode-3 behavior is the default."""
    planner = _xyz_planner()
    planner._axis_edits["x"].setText("0:1:2")
    planner._axis_edits["z"].setText("0")
    planner._build_clicked()
    pts = planner.points
    assert len(pts) == 3
    assert all(p.mach is None for p in pts)


# ── indicator strip ──────────────────────────────────────────────────────
def test_xyz_expansion_summary_counts_without_auto_zero(app):
    planner = _xyz_planner()
    planner._axis_edits["x"].setText("0:1:2")
    planner._axis_edits["mach"].setText("0.3")
    summary = planner._expansion_summary()
    assert "M[0.3]" in summary
    assert summary.endswith("3 pts")         # 3 x × 1 speed — no 0 added
    planner.set_speed_unit("rpm")
    planner._axis_edits["mach"].setText("600")
    summary = planner._expansion_summary()
    assert "N[600]" in summary               # symbol follows the unit
    assert summary.endswith("3 pts")
