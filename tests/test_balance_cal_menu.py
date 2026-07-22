"""Advanced ▸ Balance Calibration opens the balcal_gui window."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication            # noqa: E402

from freestream.config import FreestreamConfig       # noqa: E402
from freestream.manager import DeviceManager         # noqa: E402
from freestream.app.main_window import FreestreamMainWindow  # noqa: E402

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
    config = FreestreamConfig(operator="pytest", config_name="balcal",
                              data_root=str(tmp_path / "runs"))
    win = FreestreamMainWindow(config, manager=manager)
    win.show()
    app.processEvents()
    yield win
    if getattr(win, "_balcal_win", None) is not None:
        win._balcal_win.close()
    win.close()
    app.processEvents()


def test_advanced_menu_exists(window):
    titles = [a.text() for a in window.menuBar().actions()
              if a.menu() is not None]
    assert "&Advanced" in titles


def test_open_balance_cal_window(window, app):
    window._open_balance_cal()
    app.processEvents()
    assert getattr(window, "_balcal_win", None) is not None
    assert window._balcal_win.isVisible()
    # not connected in freestream → the window owns its own (future) DAQ
    assert window._balcal_win._external_device is None
    # second invocation re-uses the same window
    first = window._balcal_win
    window._open_balance_cal()
    assert window._balcal_win is first
