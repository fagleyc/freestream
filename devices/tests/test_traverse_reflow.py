"""Traverse panel scalability: the three axis cards + "All axes" box reflow
between 1x4 / 2x2 / stacked with the panel width, the Connection bar's two
clusters stack when narrow, and the Diagnostics/Calibration tabs are
scroll-wrapped so their fixed-width rows never pin the window's minimum.
Offscreen Qt, no hardware.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PyQt6.QtWidgets import QApplication, QGroupBox, QScrollArea

from traverse_swt import theme
from traverse_swt.config import TraverseConfig
from traverse_swt.app.main_window import (_AxisCard, _ReflowCards,
                                          _ReflowRow, TraversePanel)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv[:1])


def _panel(app):
    return TraversePanel(TraverseConfig(force_sim=True))


def test_container_holds_cards_and_side_box(app):
    panel = _panel(app)
    cont = panel.cards_container
    assert isinstance(cont, _ReflowCards)
    for name in "XYZ":
        assert panel.cards[name].parent() is cont
    assert panel.side_group.parent() is cont


def test_reflow_modes_by_width(app):
    panel = _panel(app)
    cont = panel.cards_container
    assert cont.two_col_threshold < cont.wide_threshold
    cont.reflow(cont.wide_threshold + 100)
    assert cont.columns == 4
    cont.reflow((cont.two_col_threshold + cont.wide_threshold) // 2)
    assert cont.columns == 2
    cont.reflow(cont.two_col_threshold - 50)
    assert cont.columns == 1


def test_minimum_hint_is_one_card_wide(app):
    # the container must report a ONE-box minimum so the window can be
    # shrunk below the wide row's width — that shrink drives the reflow
    panel = _panel(app)
    cont = panel.cards_container
    widest = max(max(w.minimumSizeHint().width(), w.minimumWidth())
                 for w in ([panel.cards[n] for n in "XYZ"]
                           + [panel.side_group]))
    assert cont.minimumSizeHint().width() == widest
    assert cont.minimumSizeHint().width() < cont.wide_threshold


def test_resizeevent_drives_reflow(app):
    # a standalone (top-level) container honours resize() → its
    # resizeEvent picks the arrangement, exactly as the window does
    cards = [_AxisCard(n, theme.series_color(i))
             for i, n in enumerate("XYZ")]
    cont = _ReflowCards(cards, QGroupBox("All axes"))
    cont.show()
    cont.resize(cont.wide_threshold + 200, 400)
    app.processEvents()
    assert cont.columns == 4
    # a TOP-LEVEL widget's minimum tracks its CURRENT arrangement, so a
    # big jump clamps mid-way (4→2) and a second shrink — exactly like a
    # real drag — finishes the collapse (2→1). The embedded panel has no
    # such clamp: the container's one-card minimumSizeHint governs there.
    target = max(cont.two_col_threshold - 60, 60)
    for _ in range(3):
        cont.resize(target, 400)
        app.processEvents()
    assert cont.columns == 1
    cont.hide()


def test_connection_bar_clusters_stack_when_narrow(app):
    panel = _panel(app)
    row = panel.conn_row
    assert isinstance(row, _ReflowRow)
    row.reflow(row.threshold + 200)
    assert row.stacked is False
    row.reflow(row.threshold - 50)
    assert row.stacked is True
    # every control stays wired in either arrangement (no reparenting)
    assert panel.connect_btn.text() == "Connect"
    assert panel.ip_edit.isEnabled()


def test_diag_and_cal_tabs_are_scroll_wrapped(app):
    panel = _panel(app)
    pages = [panel.tabs.widget(i) for i in range(panel.tabs.count())]
    scrolls = [p for p in pages if isinstance(p, QScrollArea)]
    assert len(scrolls) == 2
    wrapped = {s.widget() for s in scrolls}
    assert panel.diag_panel in wrapped
    assert panel.cal_panel in wrapped


def test_cards_functional_after_reflow_cycles(app):
    panel = _panel(app)
    cont = panel.cards_container
    for width in (cont.wide_threshold + 100, cont.two_col_threshold - 50,
                  cont.wide_threshold + 100):
        cont.reflow(width)
    for name in "XYZ":
        card = panel.cards[name]
        assert card.move_btn.text() == "Move"
        assert card.home_btn.text() == "Home"
        assert card.stop_btn.text().endswith("STOP")
    assert panel.estop_btn.text().endswith("E-STOP")
