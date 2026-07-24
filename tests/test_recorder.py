"""Tests for freestream.recorder.Hdf5Recorder — the §5 file contract."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import h5py
import numpy as np
import pytest

# bootstrap: make projects/freestream importable regardless of cwd
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freestream.recorder import Hdf5Recorder, read_point  # noqa: E402

RATE_SB, RATE_DB, RATE_POS = 1000.0, 100.0, 50.0
T_START = datetime(2026, 7, 7, 14, 30, 0)


def make_blocks(n_sb=1000, n_db=100, n_pos=50):
    rng = np.random.default_rng(42)
    return {
        "StrainBook_0": {ch: rng.normal(size=n_sb) for ch in
                         ("N1", "N2", "Y1", "Y2", "Axial", "Roll",
                          "Excitation")},
        "DaqBook2005": {ch: rng.normal(size=n_db) for ch in
                        ("Pdiff", "Ptot", "Temp")},
        "Positioner": {"Alpha": np.full(n_pos, -2.0),
                       "Beta": np.zeros(n_pos)},
    }


RATES = {"StrainBook_0": RATE_SB, "DaqBook2005": RATE_DB,
         "Positioner": RATE_POS}

UNITS = {"StrainBook_0": {ch: "V" for ch in
                          ("N1", "N2", "Y1", "Y2", "Axial", "Roll",
                           "Excitation")},
         "DaqBook2005": {"Pdiff": "V", "Ptot": "V", "Temp": "V"},
         "Positioner": {"Alpha": "deg", "Beta": "deg"}}


@pytest.fixture
def rec(tmp_path):
    return Hdf5Recorder(tmp_path, config_name="cfgA")


def write_default(rec, **kw):
    args = dict(
        point_meta={"alpha": -2.0, "beta": 0.0, "t_start": T_START,
                    "flap_deg": 12.5},
        blocks=make_blocks(), rates=RATES, channel_units=UNITS,
    )
    args.update(kw)
    return rec.write_point(**args)


# ── run numbering ────────────────────────────────────────────────────────
def test_next_run_number_empty_dir(rec):
    assert rec.next_run_number() == 1


def test_next_run_number_scans_existing_files(rec):
    for name in ("run_0001_alpha_0.0_beta_0.0_AirOn.h5",
                 "run_0003_alpha_2.0_beta_0.0_AirOff.h5",
                 "run_0007_AirOn.h5",
                 "notes.txt"):                       # non-run file ignored
        (rec.config_dir / name).touch()
    assert rec.next_run_number() == 8


def test_write_uses_next_run_number_and_increments(rec):
    p1 = write_default(rec)
    p2 = write_default(rec)
    assert p1.name.startswith("run_0001_")
    assert p2.name.startswith("run_0002_")


def test_folder_per_configuration(tmp_path):
    rec = Hdf5Recorder(tmp_path, config_name="flap12")
    p = write_default(rec)
    assert p.parent == tmp_path / "flap12"
    assert read_point(p)["attrs"]["config_name"] == "flap12"


# ── filename construction ────────────────────────────────────────────────
def test_filename_with_mach(rec):
    p = write_default(rec, point_meta={"alpha": -2.0, "beta": 0.0,
                                       "mach": 0.3, "t_start": T_START})
    # mach token always renders {m:.2f}; air state is not a filename token
    assert p.name == "run_0001_alpha_-2.0_beta_0.0_mach_0.30.h5"


def test_filename_omits_missing_mach(rec):
    p = write_default(rec)
    assert p.name == "run_0001_alpha_-2.0_beta_0.0.h5"
    assert "mach" not in p.name


def test_filename_rpm_override_not_in_name(rec):
    # a direct-RPM override is meta only — never a filename token
    p = write_default(rec, point_meta={"alpha": 1.0, "rpm": 600.0,
                                       "t_start": T_START})
    assert p.name == "run_0001_alpha_1.0.h5"
    assert read_point(p)["attrs"]["rpm"] == 600.0


# ── selected-speed-unit filename token (Casey's Hz sweep) ────────────────
def test_filename_hz_speed_token(rec):
    # a non-mach entry unit drives the speed token: {TAG}_{value}, in the
    # alpha, beta, speed slot — NOT a degenerate mach_0.0X
    p = write_default(rec, point_meta={
        "alpha": 0.0, "beta": 0.0, "mach": 0.0413, "t_start": T_START,
        "speed_value": 30.0, "speed_unit": "hz"})
    assert p.name == "run_0001_alpha_0.0_beta_0.0_Hz_30.0.h5"


def test_filename_hz_airoff_token(rec):
    p = write_default(rec, point_meta={
        "alpha": 0.0, "beta": 0.0, "mach": 0.0, "t_start": T_START,
        "speed_value": 0.0, "speed_unit": "hz"})
    assert p.name == "run_0001_alpha_0.0_beta_0.0_Hz_0.0.h5"


def test_filename_ftps_token(rec):
    p = write_default(rec, point_meta={
        "alpha": -2.0, "beta": 0.0, "mach": 0.15, "t_start": T_START,
        "speed_value": 50.0, "speed_unit": "ft/s"})
    assert p.name == "run_0001_alpha_-2.0_beta_0.0_ftps_50.0.h5"


def test_filename_rpm_unit_token_whole_number(rec):
    p = write_default(rec, point_meta={
        "alpha": 0.0, "beta": 0.0, "mach": 0.4, "t_start": T_START,
        "speed_value": 600.0, "speed_unit": "rpm"})
    assert p.name == "run_0001_alpha_0.0_beta_0.0_RPM_600.h5"


def test_filename_mach_unit_unchanged(rec):
    # speed_unit == "mach" keeps the historic mach token verbatim
    p = write_default(rec, point_meta={
        "alpha": -2.0, "beta": 0.0, "mach": 0.30, "t_start": T_START,
        "speed_value": 0.30, "speed_unit": "mach"})
    assert p.name == "run_0001_alpha_-2.0_beta_0.0_mach_0.30.h5"


def test_filename_speed_token_order_alpha_beta_speed(rec):
    # the speed token sits in the SAME slot the mach token used to (after
    # alpha and beta) — order is alpha, beta, speed
    p = write_default(rec, point_meta={
        "alpha": 3.0, "beta": 1.0, "mach": 0.06, "t_start": T_START,
        "speed_value": 20.0, "speed_unit": "hz"})
    tokens = p.stem.split("_")
    assert tokens.index("alpha") < tokens.index("beta") < tokens.index("Hz")


def test_filename_speed_token_in_template(rec_dir_tmpl):
    p = rec_dir_tmpl.filename_for(1, {"mach": 0.06, "speed_value": 30.0,
                                      "speed_unit": "hz"})
    assert p == "run_0001_Hz_30.0.h5"


@pytest.fixture
def rec_dir_tmpl(tmp_path):
    return Hdf5Recorder(tmp_path, config_name="tmpl",
                        filename_template="run_{run}_{speed}")


def test_filename_omits_missing_alpha_beta(rec):
    p = write_default(rec, point_meta={"t_start": T_START})
    assert p.name == "run_0001.h5"


def test_filename_air_off(rec):
    p = write_default(rec, air_state="AirOff")
    # air state is no longer a filename token — stored as a root attr only
    assert "AirOff" not in p.name and "AirOn" not in p.name
    assert read_point(p)["attrs"]["air_state"] == "AirOff"


def test_filename_explicit_run_number(rec):
    p = write_default(rec, run_number=42)
    assert p.name.startswith("run_0042_")
    assert rec.next_run_number() == 43


# ── schema: groups, datasets, waveform attrs ─────────────────────────────
def test_schema_groups_and_waveform_attrs(rec):
    p = write_default(rec)
    with h5py.File(p, "r") as f:
        for g in ("StrainBook_0", "DaqBook2005", "Positioner", "Time",
                  "meta"):
            assert g in f, f"missing group {g}"
        n1 = f["StrainBook_0/N1"]
        assert n1.dtype == np.float64
        assert n1.attrs["wf_increment"] == pytest.approx(1.0 / RATE_SB)
        assert n1.attrs["wf_samples"] == 1000
        start = n1.attrs["wf_start_time"]
        if isinstance(start, bytes):
            start = start.decode()
        assert start == T_START.isoformat()
        unit = n1.attrs["unit"]
        assert (unit.decode() if isinstance(unit, bytes) else unit) == "V"
        # a second group gets ITS group's rate
        assert f["DaqBook2005/Pdiff"].attrs["wf_increment"] == \
            pytest.approx(1.0 / RATE_DB)
        assert f["Positioner/Alpha"].attrs["wf_samples"] == 50


def test_time_axis_matches_longest_group(rec):
    p = write_default(rec)
    with h5py.File(p, "r") as f:
        t = f["Time/Time"][()]
        assert len(t) == 1000                      # longest = StrainBook_0
        assert t[0] == 0.0
        assert np.allclose(np.diff(t), 1.0 / RATE_SB)
        assert t[-1] == pytest.approx(999 / RATE_SB)


def test_explicit_time_array(rec):
    t_in = np.linspace(0.0, 2.0, 21)
    p = write_default(rec, time_array=t_in)
    np.testing.assert_allclose(read_point(p)["groups"]["Time"]["Time"], t_in)


# ── root attrs: fixed set + point_meta/extra_attrs verbatim ──────────────
def test_root_attrs_fixed_and_inherited(rec):
    p = write_default(
        rec,
        extra_attrs={"operator": "casey", "mode": "mode1",
                     "gear": "up", "sweep_pts": [0, 2, 4],
                     "is_repeat": True, "skip_me": None},
    )
    a = read_point(p)["attrs"]
    # fixed set
    assert a["run_number"] == 1
    assert a["config_name"] == "cfgA"
    assert a["air_state"] == "AirOn"
    assert a["mode"] == "mode1"
    assert a["operator"] == "casey"
    assert "timestamp" in a
    # point_meta verbatim, including weird custom run-sheet columns
    assert a["alpha"] == -2.0
    assert a["beta"] == 0.0
    assert a["flap_deg"] == 12.5
    # extra_attrs verbatim: str / bool / list; None skipped
    assert a["gear"] == "up"
    assert a["is_repeat"] == True                 # noqa: E712
    assert list(a["sweep_pts"]) == [0, 2, 4]
    assert "skip_me" not in a


# ── /meta/devices and /meta/config ───────────────────────────────────────
def test_meta_devices_pointers_only(rec):
    devs = [
        {"id": "strainbook", "model": "StrainBook/616", "sim": True,
         "firmware": "2.1", "cal_file": "C:/cals/sb616_2026.cal"},
        {"id": "daqbook", "model": "DaqBook/2000", "sim": False},
    ]
    p = write_default(rec, device_meta=devs)
    d = read_point(p)["devices"]
    assert set(d) == {"strainbook", "daqbook"}
    assert d["strainbook"]["model"] == "StrainBook/616"
    assert d["strainbook"]["sim"] == True         # noqa: E712
    # calibration is a POINTER string — never applied data
    assert d["strainbook"]["cal_file"] == "C:/cals/sb616_2026.cal"
    assert isinstance(d["strainbook"]["cal_file"], str)
    assert d["daqbook"]["model"] == "DaqBook/2000"


def test_meta_config_json_round_trip(rec):
    snap = {"balance": "force", "rates": {"StrainBook_0": 1000},
            "channels": ["N1", "N2"], "cal_pointer": "cals/x.cal"}
    p = write_default(rec, config_snapshot=snap)
    assert read_point(p)["config"] == snap
    with h5py.File(p, "r") as f:                  # stored as a json blob
        raw = f["meta/config"][()]
        if isinstance(raw, bytes):
            raw = raw.decode()
        assert json.loads(raw) == snap


# ── output-format dispatch ───────────────────────────────────────────────
def test_default_format_is_h5_and_writes_only_h5(rec):
    assert rec.output_format == "h5" and rec.extension == ".h5"
    p = write_default(rec)
    assert p.suffix == ".h5"
    suffixes = {q.suffix for q in rec.config_dir.iterdir()}
    assert suffixes == {".h5"}


def test_invalid_output_format_raises(tmp_path):
    with pytest.raises(ValueError, match="output_format"):
        Hdf5Recorder(tmp_path, config_name="cfgA", output_format="csv")


@pytest.mark.parametrize("fmt,ext", [("h5", ".h5"), ("mat", ".mat"),
                                     ("xlsx", ".xlsx")])
def test_filename_pattern_identical_across_formats(tmp_path, fmt, ext):
    if fmt == "mat":
        pytest.importorskip("scipy.io")
    elif fmt == "xlsx":
        pytest.importorskip("openpyxl")
    rec = Hdf5Recorder(tmp_path, config_name="cfgA", output_format=fmt)
    assert rec.output_format == fmt and rec.extension == ext
    p = write_default(rec, point_meta={"alpha": -2.0, "beta": 0.0,
                                       "mach": 0.3, "t_start": T_START})
    assert p.name == f"run_0001_alpha_-2.0_beta_0.0_mach_0.30{ext}"
    assert p.exists()
    # exactly ONE file per point — only the selected format
    assert [q.suffix for q in rec.config_dir.iterdir()] == [ext]


def test_run_numbering_scans_all_formats(tmp_path):
    pytest.importorskip("scipy.io")
    write_default(Hdf5Recorder(tmp_path, config_name="cfgA"))
    rec_mat = Hdf5Recorder(tmp_path, config_name="cfgA",
                           output_format="mat")
    assert rec_mat.next_run_number() == 2      # sees the .h5 from run 1
    p = write_default(rec_mat)
    assert p.name.startswith("run_0002_")


# ── read_point round trip ────────────────────────────────────────────────
def test_read_point_round_trip_arrays(rec):
    blocks = make_blocks()
    p = write_default(rec, blocks=blocks)
    out = read_point(p)
    for group, channels in blocks.items():
        assert set(out["groups"][group]) == set(channels)
        for ch, data in channels.items():
            np.testing.assert_array_equal(out["groups"][group][ch], data)
            ca = out["channel_attrs"][group][ch]
            for key in ("wf_increment", "wf_samples", "wf_start_time",
                        "unit"):
                assert key in ca, f"{group}/{ch} missing {key}"
            assert ca["wf_samples"] == len(data)
    # raw data is verbatim float64 — no calibration applied
    assert out["groups"]["Positioner"]["Alpha"].dtype == np.float64
