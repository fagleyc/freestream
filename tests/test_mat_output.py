"""Tests for the MATLAB .mat output format.

Covers both sides of the contract:

* freestream.recorder.Hdf5Recorder(output_format="mat") writes ONE
  primary ``.mat`` per point (no .h5), same ``run_NNNN_…`` basename,
  mirroring the HDF5 schema in MATLAB-friendly structs + a ``meta``
  struct (run params, per-channel waveform attrs, device cal POINTERS,
  config JSON, and the name sanitization map).
* Streamlined's utils.windtunnel.data_io.read_mat_file returns the same
  (RawData, properties) contract as read_hdf5_file on an .h5 of the
  same point, and read_run_file routes ``.mat`` to it.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

# --- sys.path bootstrap: make Streamlined and freestream importable --------
PROJECTS_DIR = Path(__file__).resolve().parents[2]
STREAMLINED_DIR = PROJECTS_DIR / "Streamlined"
FREESTREAM_DIR = PROJECTS_DIR / "freestream"
for p in (STREAMLINED_DIR, FREESTREAM_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

h5py = pytest.importorskip("h5py")
scipy_io = pytest.importorskip("scipy.io")

from freestream.recorder import Hdf5Recorder, _matlab_name, read_point  # noqa: E402
from utils.windtunnel import data_io  # noqa: E402
from utils.windtunnel import read_mat_file as read_mat_file_pkg  # noqa: E402
from utils.windtunnel.data_io import (  # noqa: E402
    RawData, read_hdf5_file, read_mat_file, read_run_file,
)

RATE_SB, RATE_DB, RATE_POS = 1000.0, 100.0, 50.0
RATES = {"StrainBook_0": RATE_SB, "DaqBook2005": RATE_DB,
         "Positioner": RATE_POS}
T_START = datetime(2026, 7, 7, 14, 30, 0)

UNITS = {"StrainBook_0": {ch: "V" for ch in
                          ("N1", "N2", "Y1", "Y2", "Axial", "Roll",
                           "Excitation")},
         "DaqBook2005": {"Pdiff": "V", "Ptot": "V", "Temp": "V"},
         "Positioner": {"Alpha": "deg", "Beta": "deg"}}

DEVICES = [
    {"id": "strainbook", "model": "StrainBook/616", "sim": True,
     "cal_file": "C:/cals/sb616_2026.cal"},
    {"id": "daqbook", "model": "DaqBook/2000", "sim": False,
     "cal_file": "C:/cals/db2000.pcf"},
]

CONFIG_SNAP = {"balance": "force", "rates": {"StrainBook_0": 1000},
               "cal_pointer": "cals/x.cal"}


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


def write_default(rec, **kw):
    args = dict(
        point_meta={"alpha": -2.0, "beta": 0.0, "t_start": T_START,
                    "flap_deg": 12.5},               # custom run-sheet column
        blocks=make_blocks(), rates=RATES, channel_units=UNITS,
        device_meta=DEVICES, config_snapshot=CONFIG_SNAP,
    )
    args.update(kw)
    return rec.write_point(**args)


def load_mat(path):
    return scipy_io.loadmat(str(path), squeeze_me=True,
                            struct_as_record=False)


@pytest.fixture
def rec_mat(tmp_path):
    return Hdf5Recorder(tmp_path / "mat", config_name="cfgA",
                        output_format="mat")


@pytest.fixture
def point(tmp_path, rec_mat):
    """The SAME point (seeded blocks) written in both formats:
    (h5_path, mat_path) — separate roots, identical basenames."""
    rec_h5 = Hdf5Recorder(tmp_path / "h5", config_name="cfgA")
    return write_default(rec_h5), write_default(rec_mat)


# ── output_format behavior ───────────────────────────────────────────────
def test_format_h5_default_writes_no_mat(tmp_path):
    rec = Hdf5Recorder(tmp_path, config_name="cfgA")
    assert rec.output_format == "h5"
    p = write_default(rec)
    assert p.exists() and p.suffix == ".h5"
    assert list(tmp_path.rglob("*.mat")) == []
    assert rec.last_mat_path is None


def test_format_mat_writes_only_mat(rec_mat):
    p = write_default(rec_mat)
    # the returned primary path IS the .mat — no .h5 is written at all
    assert isinstance(p, Path) and p.suffix == ".mat" and p.exists()
    assert list(rec_mat.root_dir.rglob("*.h5")) == []
    assert list(rec_mat.root_dir.rglob("*.xlsx")) == []
    assert rec_mat.last_mat_path == p


def test_mat_filename_pattern(rec_mat):
    p = write_default(rec_mat,
                      point_meta={"alpha": -2.0, "beta": 0.0, "mach": 0.3,
                                  "t_start": T_START})
    assert p.name == "run_0001_alpha_-2.0_beta_0.0_mach_0.30.mat"


def test_last_mat_path_tracks_each_point(rec_mat):
    p1 = write_default(rec_mat)
    assert rec_mat.last_mat_path == p1
    p2 = write_default(rec_mat)
    assert rec_mat.last_mat_path == p2
    assert p1 != p2
    assert p1.suffix == p2.suffix == ".mat"
    assert p2.name.startswith("run_0002_")     # numbering scans .mat too


# ── round trip: .mat arrays == .h5 arrays ────────────────────────────────
def test_mat_arrays_equal_h5_arrays(point):
    h5_path, mat_path = point
    h5 = read_point(h5_path)
    m = load_mat(mat_path)

    for group, channels in h5["groups"].items():
        assert group in m, f"missing group struct {group}"
        struct = m[group]
        assert set(struct._fieldnames) == set(channels)
        for ch, arr in channels.items():
            np.testing.assert_array_equal(
                np.asarray(getattr(struct, ch)).ravel(), arr,
                err_msg=f"{group}/{ch}")

    # synthesized Time group mirrors /Time/Time
    np.testing.assert_array_equal(
        np.asarray(m["Time"].Time).ravel(), h5["groups"]["Time"]["Time"])


# ── meta struct contents ─────────────────────────────────────────────────
def test_meta_run_params_include_custom_column(point):
    _, mat_path = point
    meta = load_mat(mat_path)["meta"]
    run = meta.run
    assert run.run_number == 1
    assert run.config_name == "cfgA"
    assert run.air_state == "AirOn"
    assert run.alpha == -2.0
    assert run.beta == 0.0
    assert run.flap_deg == 12.5                    # custom run-sheet column
    assert run.t_start == T_START.isoformat()


def test_meta_channel_waveform_attrs(point):
    _, mat_path = point
    meta = load_mat(mat_path)["meta"]
    n1 = meta.channels.StrainBook_0.N1
    assert n1.wf_increment == pytest.approx(1.0 / RATE_SB)
    assert n1.wf_samples == 1000
    assert n1.wf_start_time == T_START.isoformat()
    assert n1.unit == "V"
    assert meta.channels.DaqBook2005.Pdiff.wf_increment == \
        pytest.approx(1.0 / RATE_DB)
    alpha = meta.channels.Positioner.Alpha
    assert alpha.wf_samples == 50
    assert alpha.unit == "deg"
    # synthesized Time channel meta
    assert meta.channels.Time.Time.unit == "s"
    assert meta.channels.Time.Time.wf_increment == \
        pytest.approx(1.0 / RATE_SB)


def test_meta_devices_cal_pointers_and_config_json(point):
    _, mat_path = point
    meta = load_mat(mat_path)["meta"]
    sb = meta.devices.strainbook
    assert sb.model == "StrainBook/616"
    # calibration is a POINTER string — never applied data
    assert sb.cal_file == "C:/cals/sb616_2026.cal"
    assert meta.devices.daqbook.cal_file == "C:/cals/db2000.pcf"
    assert json.loads(meta.config_json) == CONFIG_SNAP


# ── name sanitization + mapping ──────────────────────────────────────────
def test_matlab_name_sanitizer():
    used = set()
    assert _matlab_name("StrainBook_0") == "StrainBook_0"
    assert _matlab_name("2Fast 4U") == "x2Fast_4U"
    assert _matlab_name("Roll Rate (deg/s)") == "Roll_Rate__deg_s_"
    assert _matlab_name("_leading") == "x_leading"
    assert _matlab_name("") == "x"
    # collision dedup
    assert _matlab_name("a b", used) == "a_b"
    assert _matlab_name("a_b", used) == "a_b_2"
    assert _matlab_name("a/b", used) == "a_b_3"
    # 63-char MATLAB limit
    assert len(_matlab_name("x" * 100)) == 63


def test_sanitized_names_and_name_map_in_meta(tmp_path):
    rec = Hdf5Recorder(tmp_path, config_name="cfgB", output_format="mat")
    blocks = {
        "2Fast 4U": {"1X": np.arange(10.0),
                     "Roll Rate (deg/s)": np.arange(10.0) * 2},
        "StrainBook_0": {"N1": np.arange(20.0)},
    }
    rates = {"2Fast 4U": 10.0, "StrainBook_0": 20.0}
    mat_path = rec.write_point(
        point_meta={"alpha": 1.0, "Test Point #": 3},
        blocks=blocks, rates=rates)
    m = load_mat(mat_path)

    assert "x2Fast_4U" in m
    struct = m["x2Fast_4U"]
    assert set(struct._fieldnames) == {"x1X", "Roll_Rate__deg_s_"}
    np.testing.assert_array_equal(
        np.asarray(struct.x1X).ravel(), blocks["2Fast 4U"]["1X"])

    nm = m["meta"].name_map
    assert nm.groups.x2Fast_4U == "2Fast 4U"
    assert nm.groups.StrainBook_0 == "StrainBook_0"
    assert nm.channels.x2Fast_4U.x1X == "1X"
    assert nm.channels.x2Fast_4U.Roll_Rate__deg_s_ == "Roll Rate (deg/s)"
    # run-attr keys mapped too (custom column with illegal chars)
    run_map = {k: getattr(nm.run, k) for k in nm.run._fieldnames}
    assert run_map["Test_Point__"] == "Test Point #"
    assert m["meta"].run.Test_Point__ == 3


# ── Streamlined reader: parity with read_hdf5_file ───────────────────────
def test_read_mat_file_matches_read_hdf5_file(point):
    h5_path, mat_path = point
    h5_raw, h5_props = read_hdf5_file(str(h5_path))
    mat_raw, mat_props = read_mat_file(str(mat_path))

    assert isinstance(mat_raw, RawData)
    assert set(mat_raw.data.keys()) == set(h5_raw.data.keys())
    np.testing.assert_array_equal(mat_raw.time, h5_raw.time)
    for name in h5_raw.data:
        # fast channels bit-equal; slow ones interpolated identically
        # (same float64 inputs, same interp1d call) -> also bit-equal
        np.testing.assert_array_equal(mat_raw.data[name],
                                      h5_raw.data[name], err_msg=name)
    # run params surface via properties with original key names
    assert set(mat_raw.properties.keys()) == set(h5_raw.properties.keys())
    for key in ("run_number", "config_name", "air_state", "alpha",
                "flap_deg"):
        assert mat_raw.properties[key] == h5_raw.properties[key], key
    assert mat_props == h5_props


def test_read_mat_file_resampling_to_fastest(rec_mat):
    """Slow Positioner channels land on the fast time base (same
    semantics as read_hdf5_file / read_tdms_file)."""
    mat_path = write_default(rec_mat)
    raw, _ = read_mat_file(str(mat_path))
    assert len(raw.time) == 1000
    assert np.isclose(raw.time[1] - raw.time[0], 1.0 / RATE_SB)
    for name in ("N1", "Pdiff", "Alpha", "Beta"):
        assert len(raw.data[name]) == 1000, name
    # Alpha was constant -2.0 at 50 Hz; interpolation preserves the level
    in_span = raw.time <= 49 / RATE_POS
    assert np.allclose(raw.data["Alpha"][in_span], -2.0, atol=1e-6)
    # Time/meta never leak into channel data
    assert "Time" not in raw.data
    assert not any(k.lower().startswith("meta") for k in raw.data)


def test_read_mat_file_unsanitizes_channel_keys(tmp_path):
    """Channel keys come back under their ORIGINAL names (via
    meta.name_map), matching read_hdf5_file on the sibling .h5."""
    rec_h5 = Hdf5Recorder(tmp_path / "h5", config_name="cfgC")
    rec_mat = Hdf5Recorder(tmp_path / "mat", config_name="cfgC",
                           output_format="mat")
    blocks = {
        "2Fast 4U": {"1X": np.sin(np.arange(50.0))},
        "StrainBook_0": {"N1": np.cos(np.arange(100.0))},
    }
    args = dict(point_meta={"alpha": 0.0, "beta": 0.0}, blocks=blocks,
                rates={"2Fast 4U": 50.0, "StrainBook_0": 100.0})
    h5_path = rec_h5.write_point(**args)
    mat_path = rec_mat.write_point(**args)
    h5_raw, _ = read_hdf5_file(str(h5_path))
    mat_raw, _ = read_mat_file(str(mat_path))
    assert set(mat_raw.data.keys()) == set(h5_raw.data.keys())
    assert "1X" in mat_raw.data
    for name in h5_raw.data:
        np.testing.assert_array_equal(mat_raw.data[name],
                                      h5_raw.data[name], err_msg=name)


def test_read_run_file_routes_mat(point):
    _, mat_path = point
    raw, props = read_run_file(str(mat_path))
    assert isinstance(raw, RawData)
    direct_raw, direct_props = read_mat_file(str(mat_path))
    assert set(raw.data.keys()) == set(direct_raw.data.keys())
    np.testing.assert_array_equal(raw.data["N1"], direct_raw.data["N1"])
    assert props == direct_props


def test_read_mat_file_exported_from_package(point):
    _, mat_path = point
    raw, _ = read_mat_file_pkg(str(mat_path))
    assert isinstance(raw, RawData)


def test_missing_scipy_raises_clear_importerror(point, monkeypatch):
    _, mat_path = point
    monkeypatch.setattr(data_io, "MAT_AVAILABLE", False)
    with pytest.raises(ImportError, match="scipy"):
        read_mat_file(str(mat_path))
