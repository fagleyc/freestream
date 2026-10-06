"""LSWT traverse window scalability: the three axis cards + E-STOP box
reflow between 1x4 / 2x2 / stacked with the window width, the Connection
bar's two clusters stack when narrow, live-value labels and the ±1000"
spinboxes no longer bind the layout minimums, and the window honors
shrinking to well under 800 px. Offscreen Qt, no hardware.
"""

import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest
from PyQt6.QtWidgets import QApplication, QGroupBox, QSizePolicy

from lswt_traverse.config import TraverseConfig
from lswt_traverse.app.main_window import (_AxisCard, _ReflowCards,
                                           _ReflowRow, TraverseMainWindow)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication(sys.argv[:1])


@pytest.fixture()
def win(app):
    w = TraverseMainWindow(TraverseConfig(force_sim=True))
    yield w
    w.device.disconnect()
    w.deleteLater()
    app.processEvents()


def test_container_holds_cards_and_side_box(win):
    cont = win.cards_container
    assert isinstance(cont, _ReflowCards)
    for name in "XYZ":
        assert win.cards[name].parent() is cont
    assert win.side_box.parent() is cont


def test_reflow_modes_by_width(win):
    cont = win.cards_container
    assert cont.two_col_threshold < cont.wide_threshold
    cont.reflow(cont.wide_threshold + 100)
    assert cont.columns == 4
    cont.reflow((cont.two_col_threshold + cont.wide_threshold) // 2)
    assert cont.columns == 2
    cont.reflow(cont.two_col_threshold - 50)
    assert cont.columns == 1


def test_minimum_hint_is_one_card_wide(win):
    # the container must report a ONE-box minimum so the window can be
    # shrunk below the wide row's width — that shrink drives the reflow
    cont = win.cards_container
    widest = max(max(w.minimumSizeHint().width(), w.minimumWidth())
                 for w in ([win.cards[n] for n in "XYZ"]
                           + [win.side_box]))
    assert cont.minimumSizeHint().width() == widest
    assert cont.minimumSizeHint().width() < cont.wide_threshold


def test_resizeevent_drives_reflow(app):
    # a standalone (top-level) container honours resize() → its
    # resizeEvent picks the arrangement, exactly as the window does
    cards = [_AxisCard(n) for n in "XYZ"]
    cont = _ReflowCards(cards, QGroupBox("E-STOP"))
    cont.show()
    cont.resize(cont.wide_threshold + 200, 400)
    app.processEvents()
    assert cont.columns == 4
    # a TOP-LEVEL widget's minimum tracks its CURRENT arrangement, so a
    # big jump clamps mid-way (4→2) and a second shrink — exactly like a
    # real drag — finishes the collapse (2→1). The embedded container
    # has no such clamp: its one-card minimumSizeHint governs there.
    target = max(cont.two_col_threshold - 60, 60)
    for _ in range(3):
        cont.resize(target, 400)
        app.processEvents()
    assert cont.columns == 1
    cont.hide()


def test_connection_bar_clusters_stack_when_narrow(win):
    row = win.conn_row
    assert isinstance(row, _ReflowRow)
    row.reflow(row.threshold + 200)
    assert row.stacked is False
    row.reflow(row.threshold - 50)
    assert row.stacked is True
    # every control stays wired in either arrangement (no reparenting)
    assert win.connect_btn.text() == "Connect"
    assert win.port_edit.isEnabled()


def test_live_values_do_not_bind_card_minimums(win, app):
    # live readouts (24pt position, fault text, HOME SET) and the ±1000"
    # spin ranges must not set the card minimums — otherwise every value
    # change breathes the reflow thresholds
    card = win.cards["X"]
    for lbl in (card.big_lbl, card.state_lbl, card.ref_lbl):
        assert (lbl.sizePolicy().horizontalPolicy()
                == QSizePolicy.Policy.Ignored)
    for sp in (card.target, card.min_spin, card.max_spin):
        assert (sp.sizePolicy().horizontalPolicy()
                == QSizePolicy.Policy.Ignored)
        assert sp.minimumWidth() >= 60          # still usable, never 0
    before = card.minimumSizeHint().width()
    card.set_state({"position": -123.456, "moving": False,
                    "referenced": True, "fault": "chain timeout unit 3",
                    "state": "FAULT: chain timeout unit 3"})
    app.processEvents()
    assert card.minimumSizeHint().width() == before


def test_window_shrinks_well_under_800(win, app):
    # sane default (not the old ~2340 px jail), and a real-drag shrink —
    # a top-level window clamps to its CURRENT arrangement, so step down
    # like a user would; each step unlocks the next narrower mode
    assert win.width() <= 1200
    win.show()
    app.processEvents()
    for target in (1100, 900, 730, 730):
        win.resize(target, 700)
        app.processEvents()
    assert win.cards_container.columns == 1
    assert win.conn_row.stacked is True
    assert win.width() <= 760
    assert win.minimumSizeHint().width() < 800
    win.hide()


def test_cards_functional_after_reflow_cycles(win):
    cont = win.cards_container
    for width in (cont.wide_threshold + 100, cont.two_col_threshold - 50,
                  cont.wide_threshold + 100):
        cont.reflow(width)
    for name in "XYZ":
        card = win.cards[name]
        assert card.move_btn.text() == "Move"
        assert card.stop_btn.text() == "STOP"
        assert card.home_btn.text() == "Set home here"
        assert card.jog_neg.text().endswith("jog")
    assert win.estop_btn.text() == "E-STOP"
