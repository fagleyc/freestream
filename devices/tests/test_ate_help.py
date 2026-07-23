"""Help/About coverage for ate_balance: about metadata, Help menu, docs."""

import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from ate_balance import about

DOCS = (Path(__file__).resolve().parents[1] / "ate_balance" / "docs" /
        "index.html")


def test_about_metadata():
    assert re.fullmatch(r"\d+\.\d+\.\d+", about.__version__)
    assert about.APP_NAME
    assert about.AUTHOR == "C. Fagley"
    assert about.CONTACT == "casey.fagley@afacademy.af.edu"
    assert about.VERSION_HISTORY
    assert about.VERSION_HISTORY[0][0] == about.__version__
    for version, date, summary in about.VERSION_HISTORY:
        assert version and date and summary


def test_package_version_matches_about():
    import ate_balance
    assert ate_balance.__version__ == about.__version__


def test_docs_exist_and_mention_app():
    assert DOCS.is_file()
    text = DOCS.read_text(encoding="utf-8")
    assert about.APP_NAME in text
    assert about.CONTACT in text


def test_help_menu_and_about_dialog():
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication
    from ate_balance.app.main_window import (AteBalanceMainWindow,
                                             _about_dialog)
    from ate_balance.config import AteConfig

    app = QApplication.instance() or QApplication([sys.argv[0]])
    win = AteBalanceMainWindow(AteConfig(force_sim=True))
    try:
        menus = [a.menu() for a in win.menuBar().actions()
                 if a.menu() is not None]
        assert menus and menus[-1].title() == "&Help"
        titles = [a.text() for a in menus[-1].actions()
                  if not a.isSeparator()]
        assert "&Documentation" in titles
        assert any(t.startswith("&About") for t in titles)

        dlg = _about_dialog(win)
        assert about.APP_NAME in dlg.windowTitle()
        dlg.deleteLater()
    finally:
        win.close()
        app.processEvents()
