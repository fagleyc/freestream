"""
Round-trip test: Freestream-schema HDF5 files must be readable by
Streamlined's read_hdf5_file() and produce the same structure that
read_tdms_file() produces (see WindTunnelSuite_BuildPrompt.md section 5.3).
"""

import sys
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

from utils.windtunnel import data_io  # noqa: E402
from utils.windtunnel.data_io import RawData, read_hdf5_file, read_run_file  # noqa: E402

# Channel layout per section 5.3 of the build prompt
STRAINBOOK_CHANNELS = ["N1", "N2", "Y1", "Y2", "Axial", "Roll", "Excitation"]
DAQBOOK_CHANNELS = ["Pdiff", "Ptot", "Temp"]
POSITIONER_CHANNELS = ["Alpha", "Beta"]
TUNNEL_CHANNELS = ["Mach_cmd", "Mach_meas", "q_meas"]
# Channels the Streamlined reader surfaces that have NO counterpart in the
# historical TDMS layout: the Tunnel group plus the reader-derived "Speed"
# dimension (data_io promotes the selected-speed axis — Hz/ftps/…/mach — to
# a first-class channel from the file's speed_value/speed_unit or the mach
# filename token). Set aside from the TDMS channel-parity comparisons.
H5_ONLY_CHANNELS = TUNNEL_CHANNELS + ["Speed"]

ROOT_ATTRS = {
    "run_number": 7,
    "timestamp": "2026-07-07T09:00:00",
    "mode": "Mode1",
    "config_name": "F16check",
    "operator": "Casey",
    "air_state": "AirOn",
    # inherited run-sheet params
    "Alpha": -2.0,
    "Beta": 0.0,
    "Mach": 0.30,
    "L1": 0.457,
}


def _write_group(h5, name, channel_names, n_samples, dt, rng):
    """Write one device group with per-dataset waveform attrs (section 5.3)."""
    grp = h5.create_group(name)
    arrays = {}
    for ch in channel_names:
        data = rng.standard_normal(n_samples)
        dset = grp.create_dataset(ch, data=data)
        dset.attrs["wf_increment"] = dt
        dset.attrs["wf_samples"] = n_samples
        dset.attrs["wf_start_time"] = "2026-07-07T09:00:00.000000"
        dset.attrs["unit"] = "V"
        arrays[ch] = data
    return arrays


def make_freestream_h5(path, n_fast=400, dt_fast=0.001,
                      n_slow=None, dt_slow=None):
    """
    Create a small HDF5 run file matching the Freestream schema (5.3):

    run_0007_...h5
      attrs: run_number, timestamp, mode, config_name, operator, air_state,
             + inherited run-sheet params
      /StrainBook_0  N1,N2,Y1,Y2,Axial,Roll,Excitation
      /DaqBook2005   Pdiff,Ptot,Temp
      /Positioner    Alpha,Beta          (was "Arc Crescent" in TDMS)
      /Tunnel        Mach_cmd,Mach_meas,q_meas
      /Time          Time
      /meta/devices, /meta/config
    Each dataset carries wf_increment, wf_samples, wf_start_time, unit.
    """
    if n_slow is None:
        n_slow = n_fast
    if dt_slow is None:
        dt_slow = dt_fast

    rng = np.random.default_rng(42)
    written = {}

    with h5py.File(path, "w") as h5:
        for key, value in ROOT_ATTRS.items():
            h5.attrs[key] = value

        written.update(_write_group(
            h5, "StrainBook_0", STRAINBOOK_CHANNELS, n_fast, dt_fast, rng))
        written.update(_write_group(
            h5, "DaqBook2005", DAQBOOK_CHANNELS, n_fast, dt_fast, rng))
        written.update(_write_group(
            h5, "Tunnel", TUNNEL_CHANNELS, n_fast, dt_fast, rng))

        # Positioner group (the TDMS "Arc Crescent" equivalent)
        pos = h5.create_group("Positioner")
        for ch, value in (("Alpha", ROOT_ATTRS["Alpha"]),
                          ("Beta", ROOT_ATTRS["Beta"])):
            data = np.full(n_slow, value) + 0.01 * rng.standard_normal(n_slow)
            dset = pos.create_dataset(ch, data=data)
            dset.attrs["wf_increment"] = dt_slow
            dset.attrs["wf_samples"] = n_slow
            dset.attrs["wf_start_time"] = "2026-07-07T09:00:00.000000"
            dset.attrs["unit"] = "deg"
            written[ch] = data

        # /Time group (skipped by the reader, like the TDMS Time group)
        time_grp = h5.create_group("Time")
        time_grp.create_dataset("Time", data=np.arange(n_fast) * dt_fast)

        # /meta bookkeeping (must not leak into channel data)
        meta = h5.create_group("meta")
        devices = meta.create_group("devices")
        devices.attrs["strainbook"] = "StrainBook/616 (sim)"
        config = meta.create_group("config")
        config.attrs["json"] = "{}"

    return written


@pytest.fixture
def h5_run(tmp_path):
    """Uniform-rate run file (all channels share the same time base)."""
    path = tmp_path / "run_0007_alpha_-2.0_beta_0.0_mach_0.30.h5"
    written = make_freestream_h5(path)
    return path, written


def test_returns_tdms_shaped_structure(h5_run):
    """Top-level contract: same tuple/type shape as read_tdms_file."""
    path, _ = h5_run
    result = read_hdf5_file(str(path))

    assert isinstance(result, tuple) and len(result) == 2
    raw, properties = result
    assert isinstance(raw, RawData)
    assert isinstance(properties, dict)
    assert isinstance(raw.data, dict)
    assert isinstance(raw.time, np.ndarray)
    assert raw.filename == str(path)
    for name, arr in raw.data.items():
        assert isinstance(arr, np.ndarray), name


def test_channel_names_match_tdms_layout(h5_run):
    """
    All device channels are flattened into raw.data keyed by channel name,
    exactly as read_tdms_file does. The Positioner group's Alpha/Beta land
    under the same keys the TDMS "Arc Crescent" group produced (group names
    are discarded by read_tdms_file, so channel-name parity is the contract).
    """
    path, _ = h5_run
    raw, _ = read_hdf5_file(str(path))

    expected = set(STRAINBOOK_CHANNELS + DAQBOOK_CHANNELS
                   + POSITIONER_CHANNELS + H5_ONLY_CHANNELS)
    assert set(raw.data.keys()) == expected
    # Time group is skipped (TDMS parity); meta groups don't leak
    assert "Time" not in raw.data
    assert not any(k.lower().startswith("meta") for k in raw.data)


def test_channel_arrays_equal(h5_run):
    """With a uniform time base, arrays come back bit-identical."""
    path, written = h5_run
    raw, _ = read_hdf5_file(str(path))

    for name, arr in written.items():
        np.testing.assert_array_equal(raw.data[name], arr, err_msg=name)


def test_time_base_from_wf_increment(h5_run):
    """
    raw.time is reconstructed from wf_increment/wf_samples the same way the
    TDMS path does (t = arange(wf_samples) * wf_increment, starting at 0).
    """
    path, written = h5_run
    raw, _ = read_hdf5_file(str(path))

    n = len(written["N1"])
    np.testing.assert_allclose(raw.time, np.arange(n) * 0.001)
    assert np.isclose(raw.time[1] - raw.time[0], 0.001)


def test_mixed_rate_channels_resampled_to_fastest(tmp_path):
    """
    Slow channels (Positioner Alpha/Beta at daq-style low rate, like the
    real Arc Crescent TDMS data) are interpolated onto the fastest time
    base, mirroring read_tdms_file's resampling.
    """
    path = tmp_path / "run_0008_alpha_5.0_beta_0.0_mach_0.30.h5"
    make_freestream_h5(path, n_fast=500, dt_fast=0.001, n_slow=50, dt_slow=0.01)

    raw, _ = read_hdf5_file(str(path))

    n_fast = 500
    assert len(raw.time) == n_fast
    for name in STRAINBOOK_CHANNELS + DAQBOOK_CHANNELS + POSITIONER_CHANNELS:
        assert len(raw.data[name]) == n_fast, name
    # Alpha was written as -2.0 + small noise; interpolation preserves the
    # level inside the slow channel's span (beyond it, cubic extrapolation
    # applies -- same as read_tdms_file's fill_value='extrapolate')
    in_span = raw.time <= 49 * 0.01
    assert np.allclose(raw.data["Alpha"][in_span], ROOT_ATTRS["Alpha"],
                       atol=0.1)
    assert np.all(np.isfinite(raw.data["Alpha"]))


def test_root_attrs_exposed_as_file_properties(h5_run):
    """
    Root attributes (run params) surface via RawData.properties, and the
    ones matching the known property names (Alpha, Beta, L1, ...) also
    appear in the returned properties dict, as with TDMS group properties.
    """
    path, _ = h5_run
    raw, properties = read_hdf5_file(str(path))

    for key, value in ROOT_ATTRS.items():
        assert key in raw.properties, key
        assert raw.properties[key] == value
    # str attrs decode to str, not bytes
    assert isinstance(raw.properties["air_state"], str)

    assert properties["Alpha"] == ROOT_ATTRS["Alpha"]
    assert properties["Beta"] == ROOT_ATTRS["Beta"]
    assert properties["L1"] == ROOT_ATTRS["L1"]


# --- air-on/off classification from the Mach token (new naming) -----------

def test_classify_by_mach_token_and_legacy_substring():
    """Freestream files drop the AirOn/AirOff token and encode the
    condition in the Mach value; legacy TDMS substrings still win first."""
    from utils.windtunnel.data_io import (classify_files_by_condition,
                                          extract_mach_from_filename)

    tare = "run_0007_alpha_0.0_beta_0.0_mach_0.00.h5"
    flow = "run_0008_alpha_-2.0_beta_0.0_mach_0.30.h5"
    legacy_on = "AirOn_F16check_Alpha_-2.0_Beta_0.0.tdms"
    legacy_off = "AirOff_F16check_Alpha_-2.0_Beta_0.0.tdms"
    noclue = "run_0009_alpha_1.0_beta_0.0.h5"        # no mach, no air token

    result = classify_files_by_condition(
        [tare, flow, legacy_on, legacy_off, noclue])

    assert tare in result["AirOff"]                  # mach_0.00 -> air off
    assert flow in result["AirOn"]                   # mach_0.30 -> air on
    assert legacy_on in result["AirOn"]              # legacy substring
    assert legacy_off in result["AirOff"]
    # a file with neither cue stays unclassified (dropped from both lists)
    assert noclue not in result["AirOn"]
    assert noclue not in result["AirOff"]

    # the helper mirrors extract_alpha_beta_from_filename's style
    assert extract_mach_from_filename(flow) == pytest.approx(0.30)
    assert extract_mach_from_filename(tare) == pytest.approx(0.0)
    assert extract_mach_from_filename(noclue) is None


def test_read_run_file_dispatches_h5(h5_run):
    """Extension dispatch routes .h5 to the HDF5 reader."""
    path, written = h5_run
    raw, properties = read_run_file(str(path))
    assert isinstance(raw, RawData)
    np.testing.assert_array_equal(raw.data["N1"], written["N1"])


def test_missing_h5py_raises_clear_importerror(h5_run, monkeypatch):
    """Without h5py, calling read_hdf5_file gives an actionable error."""
    path, _ = h5_run
    monkeypatch.setattr(data_io, "HDF5_AVAILABLE", False)
    with pytest.raises(ImportError, match="h5py"):
        read_hdf5_file(str(path))


# --- comparison against a real TDMS fixture -------------------------------

def _find_tdms_fixture():
    if not STREAMLINED_DIR.is_dir():
        return None
    for candidate in STREAMLINED_DIR.glob("*/*.tdms"):
        return candidate
    return None


def test_structure_matches_real_tdms_fixture(h5_run):
    """
    Compare the HDF5 result against read_tdms_file on a real Run fixture:
    same types, same access pattern, and the TDMS channel-key set equals
    the HDF5 key set minus the (new) Tunnel channels.
    """
    pytest.importorskip("nptdms")
    tdms_path = _find_tdms_fixture()
    if tdms_path is None:
        pytest.skip("no TDMS fixture found under Streamlined")

    from utils.windtunnel.data_io import read_tdms_file

    tdms_raw, tdms_props = read_tdms_file(str(tdms_path))
    h5_path, _ = h5_run
    h5_raw, h5_props = read_hdf5_file(str(h5_path))

    # Same top-level types
    assert type(h5_raw) is type(tdms_raw)
    assert type(h5_props) is type(tdms_props)
    assert type(h5_raw.time) is type(tdms_raw.time)
    assert type(h5_raw.data) is type(tdms_raw.data)

    # Same channel keys once the HDF5-only channels (Tunnel group + the
    # reader-derived Speed dimension) are set aside
    tdms_keys = set(tdms_raw.data.keys())
    h5_keys = set(h5_raw.data.keys()) - set(H5_ONLY_CHANNELS)
    assert h5_keys == tdms_keys

    # Per-channel data accessible identically
    for key in tdms_keys:
        assert isinstance(tdms_raw.data[key], np.ndarray)
        assert isinstance(h5_raw.data[key], np.ndarray)
        assert len(h5_raw.data[key]) == len(h5_raw.time)
    assert len(tdms_raw.data["N1"]) == len(tdms_raw.time)

    # wf_increment drives raw.time identically (dt = time[1] - time[0])
    assert np.isclose(tdms_raw.time[1] - tdms_raw.time[0], 0.001, atol=1e-6)
    assert np.isclose(h5_raw.time[1] - h5_raw.time[0], 0.001, atol=1e-6)
