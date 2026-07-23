"""Heise GUI smoke (offscreen): build, connect sim, tiles/history/unit."""

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication

from heise.app.main_window import HeiseMainWindow
from heise.config import HeiseConfig


def _pump(app, seconds):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


@pytest.fixture()
def win(app):
    w = HeiseMainWindow(HeiseConfig(force_sim=True, poll_s=0.05))
    w.show()
    app.processEvents()
    yield w
    w.close()
    app.processEvents()


def test_window_builds(win):
    panel = win.panel
    assert set(panel.tiles) == {"Pressure", "Temperature"}
    assert panel.unit_combo.count() == 13          # EUNIT codes 0..12
    assert panel.tabs.count() == 2


def test_connect_updates_tiles_and_history(win, app):
    panel = win.panel
    panel.sim.setChecked(True)
    panel._handle_connect()
    assert panel.device.connected and panel.device.sim_mode
    assert panel.lamp.text() == "SIMULATION"
    _pump(app, 0.6)
    panel._refresh_ui()
    assert "14." in panel.tiles["Pressure"].value_lbl.text()
    assert "72." in panel.tiles["Temperature"].value_lbl.text()
    assert "battery: 6.71" in panel.batt_lbl.text()
    x, y = panel.curves["Pressure"].getData()
    assert x is not None and len(x) >= 2
    panel._handle_disconnect()
    assert panel.lamp.text() == "DISCONNECTED"


def test_live_unit_switch(win, app):
    panel = win.panel
    panel.sim.setChecked(True)
    panel._handle_connect()
    _pump(app, 0.3)
    panel.unit_combo.setCurrentText("kPa")
    _pump(app, 0.4)
    panel._refresh_ui()
    assert panel.tiles["Pressure"].unit_lbl.text() == "kPa"
    assert panel.config.right.unit == "kPa"
    val = float(panel.tiles["Pressure"].value_lbl.text()
                .replace(",", "").replace("+", ""))
    assert 95.0 < val < 110.0                      # ~ambient in kPa
    # temperature tile untouched
    assert panel.tiles["Temperature"].unit_lbl.text() == "F"


def test_unit_switch_disconnected_updates_config(win):
    panel = win.panel
    panel.unit_combo.setCurrentText("mbar")
    assert panel.config.right.unit == "mbar"
    assert panel.tiles["Pressure"].unit_lbl.text() == "mbar"


def test_embedded_panel_follows_external_connect(app):
    """Embedding contract (host suites): ``device=`` + ``embedded=True``
    binds the panel to the INJECTED live gauge (never a second serial
    connection), hides the Connection row, leaves the host's on_status
    wiring alone, and the lamp/tiles follow a connect made OUTSIDE the
    panel's own buttons via the refresh timer."""
    from heise.app.main_window import HeisePanel
    from heise.device import HeiseGauge
    dev = HeiseGauge(HeiseConfig(force_sim=True, poll_s=0.05))
    panel = HeisePanel(device=dev, embedded=True)
    try:
        assert panel.device is dev                 # ONE gauge ever
        assert panel.config is dev.config
        assert not panel.conn_group.isVisibleTo(panel)
        assert dev.on_status is None               # host keeps the slot

        dev.connect()                              # the HOST connects
        _pump(app, 0.4)
        panel._refresh_ui()                        # UI-timer tick
        assert panel.lamp.text() == "SIMULATION", \
            "panel did not come alive on the host's connect"
        assert "14." in panel.tiles["Pressure"].value_lbl.text()

        dev.disconnect()                           # host disconnects…
        panel._refresh_ui()
        assert panel.lamp.text() == "DISCONNECTED"
    finally:
        panel._ui_timer.stop()
        dev.disconnect()
