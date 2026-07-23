"""User-selectable tunnel-speed units (Mach / ft/s / m/s / RPM).

The canonical tunnel axis stays Mach everywhere (SweepPoint.mach,
MachLoop, Mach_cmd) — the unit layer only translates what the operator
TYPES and READS: the Measurement Setup speed unit + tolerance, the sweep
planner's speed row, and the operator-wait request/dialog. Covers:

* freestream.speed conversion round-trips (nominal A0 / rpm_per_mach)
  and the per-unit GUI hint tables;
* config round-trip of speed_unit/speed_tolerance + old-JSON compat;
* planner grids typed in each unit → canonical Mach + entered-value
  meta (+ the rpm direct path), air-off 0 auto-prepended in EVERY unit;
* sweep operator-wait requests carrying the configured tolerance and a
  live measure in the right unit for all four units (sim manager);
* the mach unit is byte-for-byte the historical behavior.

Offscreen Qt for the GUI pieces; fakes manifest as in test_operator_wait.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freestream import speed
from freestream.config import FreestreamConfig
from freestream.derived import tunnel_state
from freestream.manager import DeviceManager
from freestream.recorder import Hdf5Recorder
from freestream.runsheet import SweepPoint
from freestream.sweep import DONE, SweepCallbacks, SweepEngine

FAKES = {
    "balance": {"adapter": "freestream._fakes.FakeStreamer", "enabled": True},
    "daq": {"adapter": "freestream._fakes.FakeDaq", "enabled": True},
    "pos": {"adapter": "freestream._fakes.FakePositioner", "enabled": True},
    "tun": {"adapter": "freestream._fakes.FakeTunnel", "enabled": True},
}
MODES = {"mode1": {"positioner": "pos", "balance": "balance",
                   "tunnel_conditions": "daq", "tunnel": "tun"}}

#: FakeDaq's constant pressures through the ONE isentropic chain
FAKE_STATE = tunnel_state(0.44, 11.38, 21.0)


def _rig(tmp_path, **cfg_kw):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"modes": MODES, "devices": FAKES}),
                        encoding="utf-8")
    mgr = DeviceManager("mode1", sim=True, manifest_path=manifest)
    mgr.connect_all()
    for s in mgr.streaming:
        s.start()
    defaults = dict(samples=50, dwell_s=0.05, move_timeout_s=5,
                    tunnel_timeout_s=5, operator="pytest")
    defaults.update(cfg_kw)
    cfg = FreestreamConfig(**defaults)
    rec = Hdf5Recorder(tmp_path / "runs", config_name="speedtest")
    return mgr, rec, cfg


# ── speed.py: conversions + hint tables ──────────────────────────────────
def test_a0_is_standard_day_speed_of_sound():
    assert speed.A0_MS == pytest.approx(340.29, abs=0.01)


@pytest.mark.parametrize("unit", speed.SPEED_UNITS)
def test_conversions_round_trip(unit):
    for mach in (0.0, 0.1, 0.3, 0.85):
        value = speed.value_from_mach(mach, unit, rpm_per_mach=1500.0)
        back = speed.mach_from(value, unit, rpm_per_mach=1500.0)
        assert back == pytest.approx(mach, abs=1e-12)


def test_known_conversion_values():
    # velocities via the FIXED standard-day A0 (planning-time nominal)
    assert speed.mach_from(340.29, "m/s") == pytest.approx(1.0, abs=1e-4)
    assert speed.mach_from(100.0, "ft/s") == \
        pytest.approx(100.0 * 0.3048 / speed.A0_MS)
    # rpm through the MachLoop's own linear map
    assert speed.mach_from(600.0, "rpm", rpm_per_mach=1500.0) == \
        pytest.approx(0.4)
    assert speed.value_from_mach(0.3, "rpm", rpm_per_mach=1500.0) == \
        pytest.approx(450.0)
    # measured-velocity display conversion is an exact unit conversion
    assert speed.convert_velocity_ms(1.0, "ft/s") == \
        pytest.approx(1.0 / 0.3048)
    assert speed.convert_velocity_ms(30.0, "m/s") == pytest.approx(30.0)


def test_unknown_unit_rejected():
    with pytest.raises(ValueError):
        speed.mach_from(1.0, "furlong/fortnight")
    with pytest.raises(ValueError):
        speed.value_from_mach(1.0, "kts")
    with pytest.raises(ValueError):
        speed.mach_from(600.0, "rpm", rpm_per_mach=0.0)


def test_gui_hint_tables_cover_every_unit():
    for table in (speed.LABELS, speed.FORMATS, speed.DEFAULT_TOLERANCES,
                  speed.SPIN_HINTS, speed.PLANNER_HINTS,
                  speed.AXIS_SYMBOLS):
        assert set(table) == set(speed.SPEED_UNITS)
    for unit in speed.SPEED_UNITS:
        lo, hi, decimals, step = speed.SPIN_HINTS[unit]
        assert lo < hi and step > 0 and decimals >= 0
        # the per-unit default tolerance must FIT its own spin range
        assert lo <= speed.DEFAULT_TOLERANCES[unit] <= hi
        # and format strings must actually format a float
        assert speed.FORMATS[unit].format(1.0)


# ── config: new fields + old-JSON compatibility ──────────────────────────
def test_config_defaults_and_json_round_trip(tmp_path):
    cfg = FreestreamConfig()
    assert cfg.speed_unit == "mach"
    assert cfg.speed_tolerance == pytest.approx(cfg.mach_tolerance)
    cfg.speed_unit = "ft/s"
    cfg.speed_tolerance = 2.5
    path = tmp_path / "cfg.json"
    cfg.save(path)
    back = FreestreamConfig.load(path)
    assert back.speed_unit == "ft/s"
    assert back.speed_tolerance == pytest.approx(2.5)


def test_old_json_without_speed_fields_inherits_mach_band(tmp_path):
    """Configs predating the unit selector carry only mach_tolerance —
    the effective band must not silently change on load."""
    path = tmp_path / "old.json"
    path.write_text(json.dumps({"operator": "casey",
                                "mach_tolerance": 0.02}),
                    encoding="utf-8")
    cfg = FreestreamConfig.load(path)
    assert cfg.speed_unit == "mach"
    assert cfg.speed_tolerance == pytest.approx(0.02)   # inherited
    assert cfg.mach_tolerance == pytest.approx(0.02)


def test_unknown_speed_unit_falls_back_to_mach(tmp_path):
    path = tmp_path / "weird.json"
    path.write_text(json.dumps({"speed_unit": "warp"}), encoding="utf-8")
    assert FreestreamConfig.load(path).speed_unit == "mach"


# ── planner: grids typed in each unit ────────────────────────────────────
from PyQt6.QtWidgets import QApplication              # noqa: E402

from freestream.app.planner import PlannerPanel       # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


def _build(planner, spec):
    planner.mach_edit.setText(spec)
    planner._build_clicked()
    return planner.points


def test_planner_mach_unit_unchanged(app):
    """The mach unit is the historical behavior EXACTLY: canonical .mach,
    no speed meta stamped (recorded attrs identical to before)."""
    planner = PlannerPanel(FreestreamConfig())
    pts = _build(planner, "0.2,0.3")
    assert [p.mach for p in pts] == [0.0, 0.2, 0.3]     # air-off 0 added
    assert all("speed_value" not in p.meta for p in pts)
    assert all("rpm" not in p.meta for p in pts)
    assert "speed [Mach]" in planner._axis_labels["mach"].text()


def test_planner_fts_unit_converts_to_canonical_mach(app):
    cfg = FreestreamConfig(speed_unit="ft/s", speed_tolerance=2.0)
    planner = PlannerPanel(cfg)
    pts = _build(planner, "100")
    # air-off 0 auto-prepended in the ENTERED unit as well
    assert [p.meta.get("speed_value") for p in pts] == [0.0, 100.0]
    assert all(p.meta.get("speed_unit") == "ft/s" for p in pts)
    assert pts[0].mach == pytest.approx(0.0)
    assert pts[1].mach == pytest.approx(100.0 * 0.3048 / speed.A0_MS)
    assert all("rpm" not in p.meta for p in pts)


def test_planner_ms_unit_converts_to_canonical_mach(app):
    cfg = FreestreamConfig(speed_unit="m/s")
    planner = PlannerPanel(cfg)
    pts = _build(planner, "30")
    assert pts[1].mach == pytest.approx(30.0 / speed.A0_MS)
    assert pts[1].meta["speed_value"] == pytest.approx(30.0)
    assert pts[1].meta["speed_unit"] == "m/s"
    assert pts[0].mach == pytest.approx(0.0)            # zero point


def test_planner_rpm_unit_routes_direct_rpm_path(app):
    cfg = FreestreamConfig(speed_unit="rpm", rpm_per_mach=1500.0)
    planner = PlannerPanel(cfg)
    pts = _build(planner, "300,600")
    assert [p.meta.get("speed_value") for p in pts] == [0.0, 300.0, 600.0]
    # rpm entries ride the engine's documented direct-RPM override
    assert [p.meta.get("rpm") for p in pts] == [0.0, 300.0, 600.0]
    # canonical mach still set (air-state, hysteresis keys, Mach_cmd)
    assert pts[1].mach == pytest.approx(300.0 / 1500.0)
    assert pts[2].mach == pytest.approx(600.0 / 1500.0)


def test_planner_relabels_on_unit_change(app):
    cfg = FreestreamConfig()
    planner = PlannerPanel(cfg)
    assert "speed [Mach]" in planner._axis_labels["mach"].text()
    planner.set_speed_unit("rpm")
    assert cfg.speed_unit == "rpm"                      # shared config
    assert "speed [RPM]" in planner._axis_labels["mach"].text()
    assert "RPM" in planner.mach_edit.placeholderText() \
        or "600" in planner.mach_edit.placeholderText()
    # indicator symbol follows the unit (N for rpm)
    planner.mach_edit.setText("600")
    assert "N[" in planner._expansion_summary()


# ── sweep: operator-wait requests per unit (sim manager) ─────────────────
def _run_capture(tmp_path, point, **cfg_kw):
    mgr, rec, cfg = _rig(tmp_path, **cfg_kw)
    reqs = []
    engine = SweepEngine(mgr, rec, cfg, SweepCallbacks(
        on_operator_wait=lambda r: reqs.append(r) or "proceed"))
    out = engine.run([point])[0]
    assert out.status == DONE
    return reqs[0]


def test_wait_request_mach_unit_unchanged(tmp_path):
    req = _run_capture(tmp_path,
                       SweepPoint(alpha=0.0, mach=0.3, dwell_s=0.05,
                                  samples=50))
    assert req.unit == "mach" and req.display_unit == "mach"
    assert req.target_mach == pytest.approx(0.3)
    assert req.tolerance == pytest.approx(
        FreestreamConfig().mach_tolerance)
    assert req.measure_value is None
    assert req.describe() == "Mach 0.3"


def test_wait_request_fts_unit(tmp_path):
    value = 100.0
    mach = speed.mach_from(value, "ft/s")
    pt = SweepPoint(alpha=0.0, mach=mach, dwell_s=0.05, samples=50,
                    meta={"speed_value": value, "speed_unit": "ft/s"})
    req = _run_capture(tmp_path, pt, speed_unit="ft/s",
                       speed_tolerance=2.0)
    assert req.unit == "ft/s"
    assert req.target_value == pytest.approx(100.0)
    assert req.tolerance == pytest.approx(2.0)
    assert req.target_mach == pytest.approx(mach)       # canonical kept
    # live measure in ft/s from the fake DAQ's isentropic velocity
    assert req.measure_value() == pytest.approx(
        FAKE_STATE.velocity_ms / 0.3048, rel=1e-9)
    assert req.describe() == "100 ft/s"


def test_wait_request_ms_unit(tmp_path):
    value = 30.0
    mach = speed.mach_from(value, "m/s")
    pt = SweepPoint(alpha=0.0, mach=mach, dwell_s=0.05, samples=50,
                    meta={"speed_value": value, "speed_unit": "m/s"})
    req = _run_capture(tmp_path, pt, speed_unit="m/s",
                       speed_tolerance=0.5)
    assert req.unit == "m/s"
    assert req.target_value == pytest.approx(30.0)
    assert req.tolerance == pytest.approx(0.5)
    assert req.measure_value() == pytest.approx(FAKE_STATE.velocity_ms)


def test_wait_request_velocity_without_meta_uses_nominal_map(tmp_path):
    """A run-sheet mach point under a velocity unit still gets an honest
    target: the canonical Mach through the nominal A0 map."""
    pt = SweepPoint(alpha=0.0, mach=0.2, dwell_s=0.05, samples=50)
    req = _run_capture(tmp_path, pt, speed_unit="m/s")
    assert req.target_value == pytest.approx(0.2 * speed.A0_MS)


def test_wait_request_rpm_unit_uses_speed_tolerance(tmp_path):
    pt = SweepPoint(alpha=0.0, dwell_s=0.05, samples=50,
                    meta={"rpm": 600.0, "speed_value": 600.0,
                          "speed_unit": "rpm"})
    req = _run_capture(tmp_path, pt, speed_unit="rpm",
                       speed_tolerance=10.0)
    assert req.is_rpm and req.display_unit == "rpm"
    assert req.target_rpm == pytest.approx(600.0)
    assert req.tolerance == pytest.approx(10.0)         # NOT the 1 % band


def test_rpm_override_under_mach_unit_keeps_legacy_band(tmp_path):
    """A run-sheet rpm override with the default mach unit keeps the
    historical ±1 % (≥1 RPM) band — nothing changed for old sheets."""
    pt = SweepPoint(alpha=0.0, dwell_s=0.05, samples=50,
                    meta={"rpm": 600.0})
    req = _run_capture(tmp_path, pt)
    assert req.tolerance == pytest.approx(6.0)          # 1 % of 600


def test_velocity_point_records_canonical_mach_cmd(tmp_path):
    """Recording is unit-agnostic: a velocity-unit point stamps the
    CANONICAL Mach_cmd (plus honest measured channels), as always."""
    import h5py
    value = 100.0
    mach = speed.mach_from(value, "ft/s")
    mgr, rec, cfg = _rig(tmp_path, speed_unit="ft/s", speed_tolerance=2.0)
    engine = SweepEngine(mgr, rec, cfg)
    pt = SweepPoint(alpha=0.0, mach=mach, dwell_s=0.05, samples=50,
                    meta={"speed_value": value, "speed_unit": "ft/s"})
    out = engine.run([pt])[0]
    assert out.status == DONE
    with h5py.File(out.path, "r") as f:
        assert f["Tunnel/Mach_cmd"][0] == pytest.approx(mach)
        assert f.attrs["speed_value"] == pytest.approx(100.0)
        assert f.attrs["speed_unit"] == "ft/s"


# ── setup dialog: unit combo + tolerance spin ────────────────────────────
from freestream.app.setup_dialog import MeasurementSetupDialog  # noqa: E402


def test_dialog_unit_combo_and_tolerance(app):
    cfg = FreestreamConfig()
    dlg = MeasurementSetupDialog(cfg)
    units = [dlg.speed_unit_combo.itemData(i)
             for i in range(dlg.speed_unit_combo.count())]
    assert units == list(speed.SPEED_UNITS)
    assert dlg.speed_unit_combo.currentData() == "mach"
    assert "Speed tolerance [Mach]" in dlg.speed_tol_label.text()
    # switching the unit re-ranges the spin and seeds the unit default
    dlg.speed_unit_combo.setCurrentIndex(speed.SPEED_UNITS.index("ft/s"))
    assert dlg.mach_tol_spin.value() == pytest.approx(
        speed.DEFAULT_TOLERANCES["ft/s"])
    assert "Speed tolerance [ft/s]" in dlg.speed_tol_label.text()
    dlg.mach_tol_spin.setValue(3.0)
    out = FreestreamConfig()
    dlg.apply_to(out)
    assert out.speed_unit == "ft/s"
    assert out.speed_tolerance == pytest.approx(3.0)
    assert out.mach_tolerance == pytest.approx(0.01)    # NOT clobbered


def test_dialog_open_keeps_loaded_tolerance(app):
    """Opening the dialog must NOT reset the stored tolerance — only a
    USER unit change seeds the per-unit default."""
    cfg = FreestreamConfig(speed_unit="rpm", speed_tolerance=25.0)
    dlg = MeasurementSetupDialog(cfg)
    assert dlg.speed_unit_combo.currentData() == "rpm"
    assert dlg.mach_tol_spin.value() == pytest.approx(25.0)
    out = FreestreamConfig()
    dlg.apply_to(out)
    assert out.speed_unit == "rpm"
    assert out.speed_tolerance == pytest.approx(25.0)


def test_dialog_mach_unit_mirrors_mach_tolerance(app):
    cfg = FreestreamConfig()
    dlg = MeasurementSetupDialog(cfg)
    dlg.mach_tol_spin.setValue(0.02)
    dlg.apply_to(cfg)
    assert cfg.speed_unit == "mach"
    assert cfg.speed_tolerance == pytest.approx(0.02)
    assert cfg.mach_tolerance == pytest.approx(0.02)    # mirrored


# ── wait dialog: velocity-unit display ───────────────────────────────────
from freestream.app.mach_wait_dialog import MachWaitDialog  # noqa: E402
from freestream.sweep import OperatorWaitRequest      # noqa: E402


def test_wait_dialog_speaks_velocity_unit(app):
    req = OperatorWaitRequest(
        target_mach=speed.mach_from(100.0, "ft/s"), tolerance=2.0,
        measure=lambda: (0.05, 120.0), unit="ft/s", target_value=100.0,
        measure_value=lambda: 99.0)
    dlg = MachWaitDialog(req, settle_s=60.0, sim=False)
    try:
        assert "100 ft/s" in dlg.windowTitle()
        assert dlg.target_lbl.text() == "100.0"
        assert dlg.measured_lbl.text() == "99.0"
        assert "IN TOLERANCE" in dlg.delta_lbl.text()   # |Δ|=1 ≤ 2
        assert "ft/s" in dlg.delta_lbl.text()
        # secondary line keeps the canonical Mach + fan RPM readback
        assert "Mach 0.050" in dlg.readback_lbl.text()
        assert "120 RPM" in dlg.readback_lbl.text()
    finally:
        dlg._timer.stop()
        dlg.deleteLater()


def test_wait_dialog_rpm_and_mach_unchanged(app):
    rpm_req = OperatorWaitRequest(
        target_mach=None, tolerance=10.0,
        measure=lambda: (math.nan, 595.0), target_rpm=600.0)
    dlg = MachWaitDialog(rpm_req, settle_s=60.0, sim=False)
    try:
        assert dlg.target_lbl.text() == "600"
        assert "IN TOLERANCE" in dlg.delta_lbl.text()
    finally:
        dlg._timer.stop()
        dlg.deleteLater()
    mach_req = OperatorWaitRequest(
        target_mach=0.3, tolerance=0.01,
        measure=lambda: (0.320, 450.0))
    dlg = MachWaitDialog(mach_req, settle_s=60.0, sim=False)
    try:
        assert dlg.target_lbl.text() == "0.300"
        assert "OUT OF TOLERANCE" in dlg.delta_lbl.text()
    finally:
        dlg._timer.stop()
        dlg.deleteLater()
