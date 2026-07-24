"""Balance .vol calibration INJECTION into /meta/devices/<balance_id>.

When an internal-balance adapter (StrainBook/616, NI USB-6351) has a
configured ``.vol``, ``extra_meta()`` computes the cal matrix from it and
injects the reduction contract Streamlined consumes WITHOUT opening the
.vol — ``cal_matrix`` (6*order, 6), ``cal_type``, ``cal_distances``
[x1,x2,y1,y2], ``balance_serial``, ``balance_type`` — while keeping the
``vol_path``/``cal_type`` pointer. Raw volts stay verbatim. No .vol → only
the pointer (backward compatible).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "devices"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from freestream.adapters.ni_daq import NiDaqAdapter          # noqa: E402
from freestream.adapters.strainbook import StrainbookAdapter  # noqa: E402
from freestream.recorder import Hdf5Recorder, read_point      # noqa: E402
from strainbook_616 import balcal                             # noqa: E402

VOL = _ROOT / "utilities" / "balance_cal" / "50lbCalV6.vol"
T_START = datetime(2026, 7, 7, 14, 30, 0)


def _ref_matrix(cal_type: str) -> np.ndarray:
    return balcal.calc_coeffs(balcal.read_vol_file(str(VOL)), cal_type).coeffs


# ── extra_meta() emits the full contract ─────────────────────────────────
@pytest.mark.parametrize("make", [StrainbookAdapter, NiDaqAdapter])
def test_extra_meta_emits_cal_matrix_and_distances(make):
    assert VOL.exists()
    a = make(sim=True)
    a.config.vol_path = str(VOL)
    a.config.cal_type = "Linear"
    m = a.extra_meta()

    # pointer preserved (provenance)
    assert m["vol_path"] == str(VOL)
    assert m["cal_type"] == "Linear"
    # computed contract
    cm = m["cal_matrix"]
    assert isinstance(cm, np.ndarray) and cm.dtype == np.float64
    assert cm.shape == (6, 6)                       # Linear → order 1
    assert np.allclose(cm, _ref_matrix("Linear"))
    cd = m["cal_distances"]
    assert isinstance(cd, np.ndarray) and cd.dtype == np.float64
    assert cd.shape == (4,)
    assert np.allclose(cd, [1.282, 1.2715, 1.2795, 1.267])
    assert m["balance_serial"] == "50 lb"
    assert m["balance_type"] == "internal"


@pytest.mark.parametrize("cal_type,order", [("Linear", 1),
                                            ("Quadratic", 2), ("Cubic", 3)])
def test_matrix_shape_follows_cal_type(cal_type, order):
    a = StrainbookAdapter(sim=True)
    a.config.vol_path = str(VOL)
    a.config.cal_type = cal_type
    cm = a.extra_meta()["cal_matrix"]
    assert cm.shape == (6 * order, 6)
    assert np.allclose(cm, _ref_matrix(cal_type))


def test_no_vol_path_emits_no_cal_matrix():
    """Backward compat: no .vol → empty meta, definitely no matrix."""
    a = StrainbookAdapter(sim=True)
    assert a.config.vol_path == ""
    assert a.extra_meta() == {}


def test_missing_vol_degrades_to_pointer_only():
    """A configured-but-unreadable .vol never crashes — pointer only."""
    a = StrainbookAdapter(sim=True)
    a.config.vol_path = "does_not_exist_anywhere.vol"
    a.config.cal_type = "Linear"
    m = a.extra_meta()
    assert m == {"vol_path": "does_not_exist_anywhere.vol",
                 "cal_type": "Linear"}
    assert "cal_matrix" not in m


def test_memoized_single_computation(monkeypatch):
    """The matrix is computed ONCE per (vol, cal_type), not per point."""
    from freestream.adapters import _balance_cal as bc
    bc._CACHE.clear()
    calls = {"n": 0}
    real = balcal.read_vol_file

    def counting(path):
        calls["n"] += 1
        return real(path)

    monkeypatch.setattr(balcal, "read_vol_file", counting)
    a = StrainbookAdapter(sim=True)
    a.config.vol_path = str(VOL)
    a.config.cal_type = "Linear"
    for _ in range(5):
        a.extra_meta()
    assert calls["n"] == 1


# ── recorder round-trip (HDF5) ────────────────────────────────────────────
def _blocks(n=64):
    rng = np.random.default_rng(0)
    return {"StrainBook_0": {ch: rng.normal(size=n) for ch in
                             ("N1", "N2", "Y1", "Y2", "Axial", "Roll",
                              "Excitation")}}


def _record(tmp_path, adapter, fmt="h5"):
    rec = Hdf5Recorder(tmp_path, config_name="cal", output_format=fmt)
    return rec.write_point(
        point_meta={"alpha": 0.0, "beta": 0.0, "t_start": T_START},
        blocks=_blocks(), rates={"StrainBook_0": 1000.0},
        channel_units={"StrainBook_0": {ch: "V" for ch in
                       ("N1", "N2", "Y1", "Y2", "Axial", "Roll",
                        "Excitation")}},
        device_meta=[{"id": adapter.id, "sim": adapter.sim,
                      **adapter.extra_meta()}])


def test_recorded_point_carries_cal_matrix(tmp_path):
    a = StrainbookAdapter(sim=True)
    a.config.vol_path = str(VOL)
    a.config.cal_type = "Linear"
    p = _record(tmp_path, a)

    dev = read_point(p)["devices"][a.id]
    cm = dev["cal_matrix"]
    assert isinstance(cm, np.ndarray) and cm.dtype == np.float64
    assert cm.shape == (6, 6)
    assert np.allclose(cm, _ref_matrix("Linear"))
    assert np.allclose(dev["cal_distances"], [1.282, 1.2715, 1.2795, 1.267])
    assert dev["cal_type"] == "Linear"
    assert dev["balance_serial"] == "50 lb"
    assert dev["balance_type"] == "internal"
    assert dev["vol_path"] == str(VOL)


def test_recorded_point_no_vol_has_no_matrix(tmp_path):
    a = StrainbookAdapter(sim=True)          # no vol configured
    p = _record(tmp_path, a)
    dev = read_point(p)["devices"][a.id]
    assert "cal_matrix" not in dev
    assert "vol_path" not in dev


# ── .mat mirror ───────────────────────────────────────────────────────────
def test_mat_output_mirrors_matrix(tmp_path):
    pytest.importorskip("scipy.io")
    from scipy.io import loadmat
    a = StrainbookAdapter(sim=True)
    a.config.vol_path = str(VOL)
    a.config.cal_type = "Quadratic"
    p = _record(tmp_path, a, fmt="mat")

    mat = loadmat(str(p), squeeze_me=True, struct_as_record=False)
    dev = getattr(mat["meta"].devices, a.id)
    cm = np.asarray(dev.cal_matrix)
    assert cm.shape == (12, 6)
    assert np.allclose(cm, _ref_matrix("Quadratic"))
    assert np.allclose(np.asarray(dev.cal_distances).ravel(),
                       [1.282, 1.2715, 1.2795, 1.267])
    assert str(dev.cal_type) == "Quadratic"
