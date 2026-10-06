"""The unified plot-axis engine, its control strip and the display LPF."""

import numpy as np
import pytest
from PyQt6.QtWidgets import QApplication

from freestream.app.plot_axes import (PlotAxesBar, PlotAxesEngine, display,
                                      lowpass, lowpass_trace)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture()
def group(app):
    import pyqtgraph as pg
    pw = pg.PlotWidget()
    t = np.linspace(0.0, 100.0, 501)
    curve = pw.plot(t, np.sin(t / 5.0))
    eng = PlotAxesEngine([pw.getPlotItem()], names=["sig"], time_x=True,
                         window_s=30.0)
    eng._timer.stop()                     # tests step it by hand
    yield pw, curve, eng
    eng.stop()


def _run(eng, start, seconds, step=0.033):
    now = start
    while now < start + seconds:
        now += step
        eng._tick(now, force=True)
    return now


def test_pyqtgraph_menus_are_replaced(group):
    pw, _c, _eng = group
    pi = pw.getPlotItem()
    assert not pi.getViewBox().menuEnabled()
    assert not pi.menuEnabled()


def test_time_axis_auto_follows_the_newest_window(group):
    _pw, _c, eng = group
    _run(eng, 0.0, 3.0)
    (x0, x1), (y0, y1) = eng.view(0)
    assert x1 == pytest.approx(100.0, abs=0.5)
    assert x1 - x0 == pytest.approx(30.0, abs=0.5)
    # y fits the data in that window, with a margin
    assert y0 < -0.95 and y1 > 0.95 and y1 < 1.4


def test_auto_y_glides_instead_of_jumping(group):
    _pw, curve, eng = group
    t0 = _run(eng, 0.0, 3.0)
    _, (_y0, y1_before) = eng.view(0)
    t = np.linspace(0.0, 100.0, 501)
    curve.setData(t, 10.0 * np.sin(t / 5.0))   # data suddenly 10x larger
    eng._tick(t0 + 0.033, force=True)
    _, (_a, y1_step) = eng.view(0)
    # one frame later the view has moved toward 10 but not jumped there
    assert y1_before < y1_step < 10.0
    _run(eng, t0 + 0.033, 2.0)
    _, (_a, y1_after) = eng.view(0)
    assert y1_after > 10.0                     # caught up (with margin)


def test_interval_mode_holds_the_target(group):
    _pw, curve, eng = group
    eng.set_update(5.0)
    t0 = _run(eng, 0.0, 3.0)
    _, (_a, held) = eng.view(0)
    t = np.linspace(0.0, 100.0, 501)
    curve.setData(t, 10.0 * np.sin(t / 5.0))
    t1 = _run(eng, t0, 1.0)                    # inside the 5 s hold
    _, (_a, still) = eng.view(0)
    assert still == pytest.approx(held, rel=0.02)
    _run(eng, t1, 6.0)                         # past the hold
    _, (_a, later) = eng.view(0)
    assert later > 10.0


def test_manual_range_and_back_to_auto(group):
    _pw, _c, eng = group
    eng.set_y(0, "manual", -2.0, 3.0)
    eng.set_x("manual", 10.0, 20.0)
    _run(eng, 0.0, 1.0)
    assert eng.view(0) == ((pytest.approx(10.0), pytest.approx(20.0)),
                           (pytest.approx(-2.0), pytest.approx(3.0)))
    eng.auto_all()
    _run(eng, 1.0, 3.0)
    (x0, x1), _ = eng.view(0)
    assert x1 == pytest.approx(100.0, abs=0.5)


def test_mouse_pan_switches_that_axis_to_manual(group):
    _pw, _c, eng = group
    _run(eng, 0.0, 1.0)
    vb = eng.plots[0].getViewBox()
    vb.setYRange(-5, 5, padding=0)
    eng._user_moved(0, [False, True])
    assert eng.y[0].mode == "manual" and eng.x.mode == "auto"
    assert (eng.y[0].lo, eng.y[0].hi) == (pytest.approx(-5),
                                          pytest.approx(5))


def test_bar_mirrors_and_drives_the_engine(group):
    _pw, _c, eng = group
    bar = PlotAxesBar(eng)
    bar.y_mode.buttons["manual"].click()
    assert eng.y[0].mode == "manual"
    bar.y_lo.setText("-1.5")
    bar.y_hi.setText("2.5")
    bar._y_manual()
    assert (eng.y[0].lo, eng.y[0].hi) == (-1.5, 2.5)
    bar.update_mode.setCurrentIndex(3)         # every 5 s
    assert eng.update_s == 5.0
    eng.auto_all()
    assert bar.y_mode.mode() == "auto"
    assert not bar.y_lo.isEnabled()


def test_display_lpf_is_shared_and_persisted(app):
    from freestream.config import FreestreamConfig
    cfg = FreestreamConfig(operator="t")
    display().bind(cfg)
    seen = []
    display().lpf_changed.connect(seen.append)
    display().set_lpf_hz(2.0)
    assert cfg.display_lpf_hz == 2.0 and display().lpf_hz == 2.0
    assert seen and seen[-1] == 2.0
    display().set_lpf_hz(0.0)


def test_lowpass_trace_smooths_at_the_stamped_rate():
    t = np.arange(0, 20, 0.2)                  # 5 Hz UI history
    rng = np.random.default_rng(0)
    v = 1.0 + 0.5 * rng.standard_normal(t.size)
    out = lowpass_trace(t, v, cutoff_hz=0.5)   # 10-sample average
    assert np.std(out) < 0.5 * np.std(v)
    assert lowpass_trace(t, v, cutoff_hz=0.0) is not None
    np.testing.assert_allclose(lowpass(v, 5.0, 0.0), v)
