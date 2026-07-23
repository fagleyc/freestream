"""Help-system surface for the six core device packages.

Per package: about-module metadata (__version__ / APP_NAME / AUTHOR /
CONTACT / VERSION_HISTORY), docs/index.html presence + content, and the
Help menu (Documentation + About actions, LAST menu) on each app's main
window, offscreen.
"""

import importlib
import os
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]

PACKAGES = ["traverse_swt", "tunnel_plc", "lswt", "lswt_sting",
            "ni_usb_6351", "heise"]


def _about(pkg: str):
    return importlib.import_module(f"{pkg}.about")


# ── about module surface ─────────────────────────────────────────────────

@pytest.mark.parametrize("pkg", PACKAGES)
def test_about_metadata(pkg):
    about = _about(pkg)
    assert re.fullmatch(r"\d+\.\d+\.\d+", about.__version__)
    assert about.APP_NAME.strip()
    assert about.AUTHOR == "C. Fagley"
    assert "@" in about.CONTACT
    assert about.SUMMARY.strip()
    assert len(about.VERSION_HISTORY) >= 1
    for entry in about.VERSION_HISTORY:
        version, date, summary = entry          # (version, date, summary)
        assert re.fullmatch(r"\d+\.\d+\.\d+", version)
        assert date.startswith("20")
        assert summary.strip()
    # newest first, and the newest entry IS the current version
    assert about.VERSION_HISTORY[0][0] == about.__version__


@pytest.mark.parametrize("pkg", PACKAGES)
def test_package_dunder_version_matches_about(pkg):
    mod = importlib.import_module(pkg)
    assert mod.__version__ == _about(pkg).__version__


# ── docs/index.html ──────────────────────────────────────────────────────

@pytest.mark.parametrize("pkg", PACKAGES)
def test_docs_index_exists_and_names_the_app(pkg):
    about = _about(pkg)
    docs = ROOT / pkg / "docs" / "index.html"
    assert docs.exists(), f"missing {docs}"
    text = docs.read_text(encoding="utf-8")
    assert about.APP_NAME in text
    assert about.CONTACT in text
    # all eight documented sections are anchored
    for anchor in ("#overview", "#hardware", "#start", "#gui",
                   "#config", "#safety", "#trouble", "#history"):
        assert anchor in text, f"{pkg} docs missing {anchor}"


# ── Help menu on each main window (offscreen) ────────────────────────────

pytest.importorskip("PyQt6")


@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([sys.argv[0]])


def _make_window(pkg):
    if pkg == "traverse_swt":
        from traverse_swt.app.main_window import TraverseMainWindow
        from traverse_swt.config import TraverseConfig
        return TraverseMainWindow(TraverseConfig(force_sim=True))
    if pkg == "tunnel_plc":
        from tunnel_plc.app.main_window import TunnelMainWindow
        from tunnel_plc.config import TunnelConfig
        return TunnelMainWindow(TunnelConfig(force_sim=True))
    if pkg == "lswt":
        from lswt.app.main_window import LswtMainWindow
        from lswt.config import LswtConfig
        return LswtMainWindow(LswtConfig(force_sim=True))
    if pkg == "lswt_sting":
        from lswt_sting.app.main_window import StingMainWindow
        from lswt_sting.config import StingConfig
        return StingMainWindow(StingConfig(force_sim=True))
    if pkg == "ni_usb_6351":
        from ni_usb_6351.app.main_window import NiDaqMainWindow
        from ni_usb_6351.config import NiDaqConfig
        return NiDaqMainWindow(NiDaqConfig(force_sim=True))
    from heise.app.main_window import HeiseMainWindow
    from heise.config import HeiseConfig
    return HeiseMainWindow(HeiseConfig(force_sim=True))


@pytest.mark.parametrize("pkg", PACKAGES)
def test_help_menu_is_last_with_docs_and_about(app, pkg):
    about = _about(pkg)
    win = _make_window(pkg)
    try:
        menus = [a.menu() for a in win.menuBar().actions()
                 if a.menu() is not None]
        assert menus, f"{pkg}: main window has no menus"
        help_menu = menus[-1]
        assert help_menu.title() == "&Help", \
            f"{pkg}: Help must be the LAST menu"
        texts = [a.text() for a in help_menu.actions()
                 if not a.isSeparator()]
        assert "&Documentation" in texts
        assert f"&About {about.APP_NAME}" in texts
        # the About dialog builds (and closes) cleanly
        mod = importlib.import_module(f"{pkg}.app.main_window")
        dlg = mod._AboutDialog(win)
        assert about.APP_NAME in dlg.windowTitle()
        dlg.close()
    finally:
        win.close()
        app.processEvents()
