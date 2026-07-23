"""Help menu / About dialog / documentation tests (shared template)."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication            # noqa: E402

from freestream import about                         # noqa: E402
from freestream.config import FreestreamConfig       # noqa: E402
from freestream.manager import DeviceManager         # noqa: E402
from freestream.app.main_window import (             # noqa: E402
    AboutDialog, FreestreamMainWindow)

ROOT = Path(__file__).resolve().parents[1]

FAKES = {
    "balance": {"adapter": "freestream._fakes.FakeStreamer",
                "enabled": True},
    "pos": {"adapter": "freestream._fakes.FakePositioner", "enabled": True},
}
MODES = {"mode1": {"positioner": "pos", "balance": "balance"}}


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


@pytest.fixture()
def window(app, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"modes": MODES, "devices": FAKES}),
                        encoding="utf-8")
    manager = DeviceManager("mode1", sim=True, manifest_path=manifest)
    config = FreestreamConfig(operator="pytest", config_name="helptest",
                              data_root=str(tmp_path / "runs"))
    win = FreestreamMainWindow(config, manager=manager)
    win.show()
    app.processEvents()
    yield win
    win.close()
    app.processEvents()


def test_about_metadata():
    assert about.APP_NAME == "Freestream"
    assert about.AUTHOR == "C. Fagley"
    assert about.CONTACT == "casey.fagley@afacademy.af.edu"
    assert about.VERSION_HISTORY[0][0] == about.__version__  # newest first
    assert len(about.VERSION_HISTORY) >= 6
    for version, iso_date, note in about.VERSION_HISTORY:
        assert version and iso_date and note
    import freestream
    assert freestream.__version__ == about.__version__


def test_help_menu_is_last(window):
    menus = [a.menu() for a in window.menuBar().actions()
             if a.menu() is not None]
    assert menus, "no menus found"
    assert menus[-1].title() == "&Help"
    labels = [a.text() for a in menus[-1].actions() if not a.isSeparator()]
    assert "&Documentation" in labels
    assert f"&About {about.APP_NAME}" in labels


def test_about_dialog_constructs(app):
    dlg = AboutDialog()
    assert about.APP_NAME in dlg.windowTitle()
    dlg.deleteLater()


def test_about_action_does_not_raise(window, monkeypatch):
    shown = []
    monkeypatch.setattr(AboutDialog, "exec",
                        lambda self: shown.append(self) or 0)
    help_menu = [a.menu() for a in window.menuBar().actions()
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
