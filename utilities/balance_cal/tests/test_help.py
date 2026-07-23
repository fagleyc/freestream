"""Help menu / About dialog tests for the balance_cal GUI."""

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from balcal_gui import about
from balcal_gui.app.main_window import AboutDialog, BalanceCalWindow

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture
def win(qapp):
    w = BalanceCalWindow(sim=True)
    yield w
    w.close()


def test_about_metadata():
    assert about.APP_NAME == "Balance Cal"
    assert about.AUTHOR == "C. Fagley"
    assert about.CONTACT == "casey.fagley@afacademy.af.edu"
    assert about.VERSION_HISTORY[0][0] == about.__version__
    for version, iso_date, note in about.VERSION_HISTORY:
        assert version and iso_date and note


def test_help_menu_is_last(win):
    menus = [a.menu() for a in win.menuBar().actions()
             if a.menu() is not None]
    assert menus, "no menus found"
    assert menus[-1].title() == "&Help"
    labels = [a.text() for a in menus[-1].actions() if not a.isSeparator()]
    assert "&Documentation" in labels
    assert f"&About {about.APP_NAME}" in labels


def test_about_dialog_constructs(qapp):
    dlg = AboutDialog()
    assert about.APP_NAME in dlg.windowTitle()
    dlg.deleteLater()


def test_about_action_does_not_raise(win, monkeypatch):
    shown = []
    monkeypatch.setattr(AboutDialog, "exec",
                        lambda self: shown.append(self) or 0)
    help_menu = [a.menu() for a in win.menuBar().actions()
                 if a.menu() is not None][-1]
    for action in help_menu.actions():
        if action.text().startswith("&About"):
            action.trigger()
            break
    assert len(shown) == 1


def test_documentation_file_exists():
    docs = ROOT / "docs" / "index.html"
    assert docs.is_file()
    html = docs.read_text(encoding="utf-8")
    assert about.APP_NAME in html
    assert about.CONTACT in html
