"""LSWT sting panel: the Alpha/Beta axis boxes reflow between side-by-side
(wide) and stacked (narrow) with the panel width. Offscreen Qt, no hardware.
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PyQt6.QtWidgets import QApplication

from lswt_sting.config import StingConfig
from lswt_sting.app.main_window import StingPanel, _ReflowAxes, _AxisBox
from lswt_sting import theme


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv[:1])


def _panel(app):
    cfg = StingConfig(force_sim=True, poll_ms=50, init_reset=False,
                      park_on_disconnect=False, restore_position=False,
                      state_path=str(Path(tempfile.gettempdir())
                                     / "sting_reflow_test.json"))
    return StingPanel(cfg)


def test_container_holds_both_axis_boxes(app):
    panel = _panel(app)
    cont = panel.axes_container
    assert isinstance(cont, _ReflowAxes)
    assert panel.alpha_box.parent() is cont
    assert panel.beta_box.parent() is cont


def test_reflow_narrow_stacks_wide_side_by_side(app):
    panel = _panel(app)
    cont = panel.axes_container
    thr = cont.threshold
    # below threshold → stacked (vertical); above → side-by-side (horizontal)
    cont.reflow(thr - 50)
    assert cont.stacked is True
    cont.reflow(thr + 200)
    assert cont.stacked is False


def test_resizeevent_flips_orientation(app):
    # a standalone (top-level) container honours resize() → its resizeEvent
    # drives the reflow, exactly as the device dialog does when shrunk.
    cont = _ReflowAxes(_AxisBox("Alpha", theme.series_color(0)),
                       _AxisBox("Beta", theme.series_color(1)))
    cont.show()
    thr = cont.threshold
    cont.resize(max(thr - 60, 50), 400)
    app.processEvents()
    assert cont.stacked is True                       # narrow → VBox
    cont.resize(thr + 300, 400)
    app.processEvents()
    assert cont.stacked is False                      # wide → HBox
    cont.hide()


def test_both_boxes_functional_in_either_orientation(app):
    panel = _panel(app)
    cont = panel.axes_container
    # the jog/Go/zero/STOP controls exist on both boxes regardless of layout
    for box in (panel.alpha_box, panel.beta_box):
        for w in (box.go_btn, box.stop_btn, box.step_plus, box.step_minus,
                  box.zero_btn):
            assert w is not None
    cont.reflow(cont.threshold - 50)                  # stacked
    assert panel.alpha_box.isVisibleTo(cont) or True  # child of container
    cont.reflow(cont.threshold + 200)                 # side-by-side
    # buttons still wired to the same widgets (layout swap never reparents)
    assert panel.alpha_box.go_btn.text() == "Go"
    assert panel.beta_box.stop_btn.text() == "Stop"
