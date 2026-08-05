"""Tests for the per-configuration run manifest (``manifest.json``).

The recorder maintains ONE JSON index of the whole test alongside the
``run_*`` files (schema_version 1), updated incrementally as each point
lands. Covers creation and growth, the exact contracted schema, atomic
and resumable updates, a re-taken run number replacing rather than
duplicating, quarantine of an unparseable manifest, and the hard rule
that a manifest failure NEVER interrupts an acquisition.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

# bootstrap: make projects/freestream importable regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freestream.recorder import (  # noqa: E402
    MANIFEST_NAME, MANIFEST_SCHEMA_VERSION, Hdf5Recorder,
)

RATE_SB, RATE_POS = 1000.0, 50.0
RATES = {"StrainBook_0": RATE_SB, "Positioner": RATE_POS}
T_START = datetime(2026, 7, 7, 14, 30, 0)
UNITS = {"StrainBook_0": {"N1": "V", "N2": "V"},
         "Positioner": {"Alpha": "deg"}}
CONFIG_SNAP = {"test_name": "LSWT_Test5", "ref_area": 1.5,
               "cal_pointer": "cals/x.cal"}

#: the complete top-level key set of the schema_version 1 contract
TOP_KEYS = {"schema_version", "config_name", "output_format", "created",
            "updated", "config", "points"}
#: the complete per-point key set of the contract
POINT_KEYS = {"run_number", "filename", "timestamp", "alpha", "beta",
              "mach", "speed_value", "speed_unit", "air_state", "sweep_dir"}


def make_blocks(n_sb=100, n_pos=50):
    rng = np.random.default_rng(42)
    return {"StrainBook_0": {ch: rng.normal(size=n_sb)
                             for ch in ("N1", "N2")},
            "Positioner": {"Alpha": np.full(n_pos, -2.0)}}


@pytest.fixture
def rec(tmp_path):
    return Hdf5Recorder(tmp_path, config_name="cfgA")


def write_default(rec, alpha=-2.0, **kw):
    """One point with small blocks — the manifest is what's under test."""
    args = dict(
        point_meta={"alpha": alpha, "beta": 0.0, "mach": 0.30,
                    "t_start": T_START, "speed_value": 20.0,
                    "speed_unit": "hz"},
        blocks=make_blocks(), rates=RATES, channel_units=UNITS,
        config_snapshot=CONFIG_SNAP,
    )
    args.update(kw)
    return rec.write_point(**args)


def load_manifest(rec):
    return json.loads(rec.manifest_path.read_text(encoding="utf-8"))


# ── creation and incremental growth ──────────────────────────────────────
def test_manifest_created_on_first_point(rec):
    p = write_default(rec)
    assert rec.manifest_path == rec.config_dir / MANIFEST_NAME
    assert rec.manifest_path.exists()            # lives beside the run files
    m = load_manifest(rec)
    assert m["schema_version"] == MANIFEST_SCHEMA_VERSION == 1
    assert m["config_name"] == "cfgA"
    assert m["output_format"] == "h5"
    assert len(m["points"]) == 1
    assert m["points"][0]["filename"] == p.name


def test_manifest_grows_and_stays_valid_per_point(rec):
    for i in range(4):
        write_default(rec, alpha=float(i))
        m = load_manifest(rec)                   # valid after EVERY point
        assert len(m["points"]) == i + 1
    assert [pt["run_number"] for pt in m["points"]] == [1, 2, 3, 4]
    assert [pt["alpha"] for pt in m["points"]] == [0.0, 1.0, 2.0, 3.0]
    assert all((rec.config_dir / pt["filename"]).exists()
               for pt in m["points"])


# ── schema ───────────────────────────────────────────────────────────────
def test_top_level_schema_exactly_as_contracted(rec):
    write_default(rec)
    m = load_manifest(rec)
    assert set(m) == TOP_KEYS
    assert m["config"] == CONFIG_SNAP
    datetime.fromisoformat(m["created"])         # ISO, first point written
    datetime.fromisoformat(m["updated"])         # ISO, most recent point


def test_point_schema_exactly_as_contracted(rec):
    p = write_default(rec, air_state="AirOff", point_meta={
        "alpha": 2.0, "beta": 1.0, "mach": 0.0, "t_start": T_START,
        "speed_value": 0.0, "speed_unit": "hz", "sweep_dir": "dn"})
    pt = load_manifest(rec)["points"][0]
    assert set(pt) == POINT_KEYS
    datetime.fromisoformat(pt["timestamp"])
    assert pt == {
        "run_number": 1, "filename": p.name, "timestamp": pt["timestamp"],
        "alpha": 2.0, "beta": 1.0, "mach": 0.0, "speed_value": 0.0,
        "speed_unit": "hz", "air_state": "AirOff", "sweep_dir": "dn",
    }


def test_unknown_values_are_null_never_omitted(rec):
    write_default(rec, point_meta={"alpha": 0.0, "t_start": T_START})
    pt = load_manifest(rec)["points"][0]
    assert set(pt) == POINT_KEYS                 # nothing dropped
    assert pt["beta"] is None and pt["mach"] is None
    assert pt["speed_value"] is None and pt["speed_unit"] is None
    assert pt["sweep_dir"] == ""                 # "" | "up" | "dn"
    assert pt["air_state"] == "AirOn"


def test_config_snapshot_written_once(rec):
    write_default(rec)
    write_default(rec, config_snapshot={"test_name": "SOMETHING_ELSE"})
    assert load_manifest(rec)["config"] == CONFIG_SNAP


@pytest.mark.parametrize("fmt", ["h5", "mat", "xlsx"])
def test_manifest_written_for_every_output_format(tmp_path, fmt):
    if fmt == "mat":
        pytest.importorskip("scipy.io")
    elif fmt == "xlsx":
        pytest.importorskip("openpyxl")
    rec = Hdf5Recorder(tmp_path, config_name="cfgA", output_format=fmt)
    p = write_default(rec)
    m = load_manifest(rec)
    assert m["output_format"] == fmt
    assert m["points"][0]["filename"] == p.name
    assert (rec.config_dir / m["points"][0]["filename"]).exists()


# ── ordering and re-takes ────────────────────────────────────────────────
def test_points_sorted_by_run_number(rec):
    for run in (7, 2, 5):
        write_default(rec, run_number=run)
    assert [pt["run_number"]
            for pt in load_manifest(rec)["points"]] == [2, 5, 7]


def test_retaken_run_number_replaces_not_duplicates(rec):
    write_default(rec, alpha=0.0)
    write_default(rec, alpha=2.0)
    write_default(rec, alpha=99.0, run_number=2)   # point 2 re-taken
    m = load_manifest(rec)
    assert [pt["run_number"] for pt in m["points"]] == [1, 2]
    assert m["points"][1]["alpha"] == 99.0


# ── resume ───────────────────────────────────────────────────────────────
def test_resume_appends_to_existing_manifest(tmp_path):
    rec1 = Hdf5Recorder(tmp_path, config_name="cfgA")
    write_default(rec1, alpha=0.0)
    write_default(rec1, alpha=2.0)
    rec2 = Hdf5Recorder(tmp_path, config_name="cfgA")   # test resumed
    assert rec2.next_run_number() == 3
    p = write_default(rec2, alpha=4.0)
    m = load_manifest(rec2)
    assert [pt["run_number"] for pt in m["points"]] == [1, 2, 3]
    assert [pt["alpha"] for pt in m["points"]] == [0.0, 2.0, 4.0]
    assert m["points"][-1]["filename"] == p.name


def test_resume_keeps_original_created(tmp_path):
    rec1 = Hdf5Recorder(tmp_path, config_name="cfgA")
    write_default(rec1)
    created = load_manifest(rec1)["created"]
    rec2 = Hdf5Recorder(tmp_path, config_name="cfgA")
    write_default(rec2)
    m = load_manifest(rec2)
    assert m["created"] == created
    assert m["updated"] >= created


def test_corrupt_manifest_moved_aside_and_rewritten(rec):
    rec.manifest_path.write_text("{ this is not json", encoding="utf-8")
    write_default(rec)
    bad = rec.config_dir / (MANIFEST_NAME + ".bad")
    assert bad.exists()                          # kept, not destroyed
    assert bad.read_text(encoding="utf-8") == "{ this is not json"
    m = load_manifest(rec)
    assert m["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert len(m["points"]) == 1


# ── failure containment: a manifest must never break a run ───────────────
def test_manifest_failure_does_not_propagate(rec, monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(json, "dump", boom)
    p = write_default(rec)                       # must NOT raise
    assert p.exists()                            # the point file is what
    assert not rec.manifest_path.exists()        # matters


def test_existing_manifest_never_left_corrupt(rec, monkeypatch):
    write_default(rec)
    good = rec.manifest_path.read_text(encoding="utf-8")

    def half_written(obj, fp, **kwargs):         # dies mid-serialise
        fp.write('{"schema_version": 1, "points": [')
        raise OSError("kill -9")

    monkeypatch.setattr(json, "dump", half_written)
    write_default(rec)
    monkeypatch.undo()
    assert rec.manifest_path.read_text(encoding="utf-8") == good
    assert len(load_manifest(rec)["points"]) == 1
    assert not list(rec.config_dir.glob("*.tmp"))   # no debris left behind


def test_manifest_recovers_on_the_next_point(rec, monkeypatch):
    write_default(rec)
    monkeypatch.setattr(json, "dump", lambda *a, **kw: 1 / 0)
    write_default(rec)                           # point 2's update fails
    monkeypatch.undo()
    write_default(rec)                           # point 3 writes them all
    assert [pt["run_number"]
            for pt in load_manifest(rec)["points"]] == [1, 2, 3]


# ── run numbering is blind to the manifest ───────────────────────────────
def test_next_run_number_ignores_manifest(rec):
    write_default(rec)
    assert rec.manifest_path.exists()
    assert rec.next_run_number() == 2
    # a fresh recorder over the same folder agrees
    assert Hdf5Recorder(rec.root_dir,
                        config_name="cfgA").next_run_number() == 2


def test_next_run_number_ignores_run_prefixed_json(rec):
    # belt and braces: the extension filter, not the name, does the work
    (rec.config_dir / "run_0099_notes.json").touch()
    (rec.config_dir / (MANIFEST_NAME + ".bad")).touch()
    assert rec.next_run_number() == 1


# ── balance-cal hand-off (stage_balance_cal) ─────────────────────────────
def make_vol(tmp_path, name="50lb 2026_07_24.vol"):
    vol = tmp_path / "CalFiles" / name
    vol.parent.mkdir(exist_ok=True)
    vol.write_text("Voltage Calibration File\nDate-->28-Feb-2023\n",
                   encoding="utf-8")
    return vol


def test_stage_copies_vol_and_records_manifest_entry(rec, tmp_path):
    vol = make_vol(tmp_path)
    dest = rec.stage_balance_cal(vol, {
        "cal_type": "Linear", "balance_config": "Moment",
        "balance_type": "internal", "balance_serial": "50 lb",
        "empty_is_skipped": ""})
    assert dest == rec.config_dir / vol.name     # beside the run files
    assert dest.read_text(encoding="utf-8") == \
        vol.read_text(encoding="utf-8")
    m = load_manifest(rec)
    assert m["schema_version"] == MANIFEST_SCHEMA_VERSION
    assert m["points"] == []                     # staged BEFORE any point
    assert m["balance_cal"] == {
        "vol_file": vol.name, "vol_source": str(vol),
        "cal_type": "Linear", "balance_config": "Moment",
        "balance_type": "internal", "balance_serial": "50 lb"}


def test_staged_entry_survives_point_writes(rec, tmp_path):
    vol = make_vol(tmp_path)
    rec.stage_balance_cal(vol, {"cal_type": "Linear"})
    write_default(rec)
    write_default(rec, alpha=0.0)
    m = load_manifest(rec)
    assert m["balance_cal"]["vol_file"] == vol.name
    assert len(m["points"]) == 2
    assert set(m) == TOP_KEYS | {"balance_cal"}  # additive, schema stays 1


def test_stage_missing_or_empty_vol_is_a_noop(rec, tmp_path):
    assert rec.stage_balance_cal("") is None
    assert rec.stage_balance_cal(tmp_path / "nope.vol") is None
    assert rec.stage_balance_cal(None) is None
    assert not rec.manifest_path.exists()


def test_stage_failure_never_raises(rec, tmp_path, monkeypatch):
    import freestream.recorder as recorder_mod

    def boom(*args, **kwargs):
        raise OSError("disk full")

    vol = make_vol(tmp_path)
    monkeypatch.setattr(recorder_mod.shutil, "copy2", boom)
    assert rec.stage_balance_cal(vol) is None    # logged, swallowed
