"""External Balance Calibration (Advanced ▸) — SPLAT window, sim ATE."""

import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "devices"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from PyQt6.QtWidgets import QApplication                   # noqa: E402

from freestream.app.ext_balcal import (CHANNELS,           # noqa: E402
                                       ExternalBalCalWindow, LB_PER_KG,
                                       linfit, model_compare,
                                       residual_analysis)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


def _pump(app, seconds):
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.01)


def test_linfit_and_residual_analysis():
    x = np.array([0.0, 1.0, 2.0, 3.0, 4.0])
    y = 2.0 * x + 1.0
    m, b, r2 = linfit(x, y)
    assert m == pytest.approx(2.0) and b == pytest.approx(1.0)
    assert r2 == pytest.approx(1.0)
    # error proportional to READING: residual grows with load
    rng = np.random.default_rng(1)
    loads = np.linspace(0, 10, 21)
    prop = 2.0 * loads * (1 + 0.05 * rng.standard_normal(21) * loads
                          / loads.max())
    fa = residual_analysis(loads, prop)
    assert fa["r_squared"] > 0.9
    assert fa["resid_corr"] > 0.4          # |resid| tracks |load|
    assert fa["pct_of_reading"] > 0
    # error constant (%FS): residual does NOT grow with load
    const = 2.0 * loads + 0.1 * rng.standard_normal(21)
    fc = residual_analysis(loads, const)
    assert abs(fc["resid_corr"]) < 0.5


def test_splat_session_and_mat(app, tmp_path):
    win = ExternalBalCalWindow(sim=True, data_root=str(tmp_path))
    try:
        win._connect()
        assert win.adapter.connected and win.adapter.sim
        win.dur_spin.setValue(0.4)
        win.load_spin.setValue(0.5)
        win.unit_combo.setCurrentText("kg")
        assert win._step_lb() == pytest.approx(0.5 * LB_PER_KG)

        # zero point, two up, one down — captures run on a QTimer
        for direction in ("zero", "up", "up", "down"):
            win._capture(direction)
            _pump(app, 0.7)
            assert win._cap_buf is None, "capture did not finish"
        assert len(win.steps) == 4
        step = 0.5 * LB_PER_KG
        assert [round(s["load_lb"], 4) for s in win.steps] == \
            [0.0, round(step, 4), round(2 * step, 4), round(step, 4)]
        for s in win.steps:
            assert s["n"] >= 2
            assert set(s["data"]) == set(CHANNELS)

        fits = win.analyses()
        assert all(fits[ch] is not None for ch in CHANNELS)

        # undo restores the applied-load bookkeeping
        win._undo()
        assert len(win.steps) == 3
        assert win.applied_lb == pytest.approx(2 * step)
        win._capture("down")
        _pump(app, 0.7)

        out = tmp_path / "cal.mat"
        win.save_mat(out)
        import scipy.io
        m = scipy.io.loadmat(out, squeeze_me=True,
                             struct_as_record=False)
        ate = m["ATE_Balance"]
        n_total = int(np.sum([s["n"] for s in win.steps]))
        for ch in CHANNELS:
            assert np.size(getattr(ate, ch)) == n_total
        assert np.size(ate.AppliedLoad) == n_total
        assert np.size(m["Time"].Time) == n_total
        st = m["Steps"]
        assert np.size(st.load_lb) == len(win.steps)
        assert hasattr(st.mean, "Lift") and hasattr(st.std, "Yaw")
        assert m["meta"].run.kind == "external_balance_cal"
        assert m["meta"].run.load_unit == "kg"
        f = m["Fits"]
        assert hasattr(f.Lift, "r_squared")
        assert hasattr(f.Lift, "pct_of_reading")
    finally:
        win.close()
        app.processEvents()


def test_advanced_menu_has_both_balance_cals(app, tmp_path):
    import json
    from freestream.config import FreestreamConfig
    from freestream.manager import DeviceManager
    from freestream.app.main_window import FreestreamMainWindow

    FAKES = {"balance": {"adapter": "freestream._fakes.FakeStreamer",
                         "enabled": True},
             "pos": {"adapter": "freestream._fakes.FakePositioner",
                     "enabled": True}}
    MODES = {"mode1": {"positioner": "pos", "balance": "balance"}}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"modes": MODES, "devices": FAKES}),
                        encoding="utf-8")
    mgr = DeviceManager("mode1", sim=True, manifest_path=manifest)
    cfg = FreestreamConfig(operator="pytest", config_name="extcal",
                           data_root=str(tmp_path / "runs"))
    win = FreestreamMainWindow(cfg, manager=mgr)
    try:
        adv = next(a.menu() for a in win.menuBar().actions()
                   if a.text() == "&Advanced")
        texts = [a.text() for a in adv.actions()]
        assert "&Internal Balance Calibration…" in texts
        assert "&External Balance Calibration…" in texts
        win._open_ext_balcal()
        assert win._ext_balcal_win is not None
        first = win._ext_balcal_win
        win._open_ext_balcal()                 # singleton
        assert win._ext_balcal_win is first
        first.close()
    finally:
        win.close()
        app.processEvents()


# ── %-of-reading vs %-of-full-scale discrimination ──────────────────────
# Deterministic alternating-sign error so the two hypotheses are clean:
# a proportional error must recover exponent p ~ 1, a constant error
# p ~ 0, and the RMS model comparison must pick the matching law.
_L = np.linspace(0.0, 10.0, 11)
_SIGN = np.array([1, -1] * 6)[:11]


def test_model_compare_picks_reading_for_proportional_error():
    err = 0.02 * _L * _SIGN
    mc = model_compare(_L, err, full_scale=25.0)
    assert mc["prefers"] == "reading"
    assert mc["margin"] > 40.0
    assert 0.8 < mc["p"] < 1.4          # E = c*L**p, p ~ 1
    assert mc["k"] == pytest.approx(0.02, rel=0.1)


def test_model_compare_picks_full_scale_for_constant_error():
    err = 0.05 * _SIGN
    mc = model_compare(_L, err, full_scale=25.0)
    assert mc["prefers"] == "full scale"
    assert mc["margin"] > 40.0
    assert abs(mc["p"]) < 0.2           # exponent collapses to 0
    assert mc["c"] == pytest.approx(0.05, rel=0.05)


def test_residual_analysis_reports_both_error_measures():
    """The within-step sigma is the sharper instrument: backed by every
    captured frame, it recovers the exponent exactly where the
    one-per-step residual only gets close."""
    means = 2.0 * _L + 0.02 * _L * _SIGN
    fa = residual_analysis(_L, means, stds=0.02 * _L, full_scale=25.0)
    assert fa["rdg"]["prefers"] == "reading"
    assert fa["sig"]["prefers"] == "reading"
    assert fa["sig"]["p"] == pytest.approx(1.0, abs=0.02)
    # error/reading = 0.02L / 2.0L = 1 %, independent of load
    assert fa["pct_reading_fit"] == pytest.approx(1.0, rel=0.1)

    const = 2.0 * _L + 0.05 * _SIGN
    fc = residual_analysis(_L, const, stds=np.full(_L.size, 0.05),
                           full_scale=25.0)
    assert fc["rdg"]["prefers"] == "full scale"
    assert fc["sig"]["prefers"] == "full scale"
    assert fc["pct_fs_fit"] == pytest.approx(0.2, rel=0.1)  # 0.05/25


def test_residual_analysis_without_stds_still_works():
    fa = residual_analysis(_L, 2.0 * _L + 0.05 * _SIGN)
    assert fa["sig"] is None
    assert fa["rdg"]["prefers"] == "full scale"


def test_error_model_view_draws_both_candidate_laws(app, tmp_path):
    win = ExternalBalCalWindow(sim=True, data_root=str(tmp_path))
    try:
        win._connect()
        win.dur_spin.setValue(0.3)
        for direction in ("zero", "up", "up", "up"):
            win._capture(direction)
            _pump(app, 0.6)
        win.view_combo.setCurrentText("Error model")
        _pump(app, 0.1)
        for ch in CHANNELS:
            xs, ys = win.curve_sc[ch].getData()          # |residual|
            assert xs is not None and len(xs) == len(win.steps)
            assert np.all(np.asarray(ys) >= 0)           # magnitudes
            xg, _ = win.curve_sig[ch].getData()          # sigma series
            assert xg is not None and len(xg) == len(win.steps)
            xf, yf = win.curve_fit[ch].getData()         # E = k|L|
            assert yf[0] == pytest.approx(0.0, abs=1e-12)
            xa, ya = win.curve_alt[ch].getData()         # E = c
            assert ya[0] == pytest.approx(ya[-1])
        # the verdict cell is populated for every channel
        for r in range(len(CHANNELS)):
            assert win.table.item(r, 9).text() != "--"
    finally:
        win.close()
        app.processEvents()
