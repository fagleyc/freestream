"""GUI smoke test — offscreen, fakes manifest, full sweep via the worker.

Drives QApplication.processEvents manually (pytest-qt not required).
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication            # noqa: E402

from freestream.config import FreestreamConfig        # noqa: E402
from freestream.manager import DeviceManager         # noqa: E402
from freestream.app.main_window import FreestreamMainWindow  # noqa: E402

FAKES = {
    "balance": {"adapter": "freestream._fakes.FakeStreamer", "enabled": True},
    "daq": {"adapter": "freestream._fakes.FakeDaq", "enabled": True},
    "pos": {"adapter": "freestream._fakes.FakePositioner", "enabled": True},
    "tun": {"adapter": "freestream._fakes.FakeTunnel", "enabled": True},
}
MODES = {"mode1": {"positioner": "pos", "balance": "balance",
                   "tunnel_conditions": "daq", "tunnel": "tun"}}


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


@pytest.fixture()
def window(app, tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"modes": MODES, "devices": FAKES}),
                        encoding="utf-8")
    manager = DeviceManager("mode1", sim=True, manifest_path=manifest)
    config = FreestreamConfig(operator="pytest", config_name="guitest",
                             data_root=str(tmp_path / "runs"),
                             samples=100, dwell_s=0.05,
                             move_timeout_s=5, tunnel_timeout_s=5)
    win = FreestreamMainWindow(config, manager=manager)  # injected manager
    win.show()
    app.processEvents()
    yield win
    win.close()
    app.processEvents()


def _spin(app, cond, timeout_s=60.0):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return False


def test_three_point_sweep_through_gui(app, window, tmp_path):
    win = window
    assert win.manager.devices                      # injected manager used

    # connect all through the command bar
    win.connect_btn.click()
    app.processEvents()
    assert all(st.ok for st in win.manager.all_status().values())

    # build a 3-point alpha grid programmatically via the planner
    # (dwell/samples come from the ONE config — set in the fixture)
    win.planner.alpha_edit.setText("0:1:2")
    win.planner.build_btn.click()
    app.processEvents()
    assert len(win.planner.points) == 3
    assert win.planner.table.rowCount() == 3

    # run through the GUI's worker path
    win.start_btn.click()
    assert _spin(app, lambda: win.sweep_active, timeout_s=10)
    assert _spin(app, lambda: not win.sweep_active, timeout_s=60)

    # 3 .h5 files exist
    h5_files = sorted((tmp_path / "runs").rglob("*.h5"))
    assert len(h5_files) == 3

    # planner table shows done for every row
    win.planner.refresh_statuses()
    app.processEvents()
    statuses = [win.planner.table.item(r, 4).text() for r in range(3)]
    assert statuses == ["done"] * 3

    # log contains each written file path
    log_text = win.console.toPlainText()
    for path in h5_files:
        assert str(path) in log_text

    # banner never appeared
    assert not win.banner.isVisible()


def test_blockers_banner_refuses_start(app, window, tmp_path):
    win = window
    win.connect_btn.click()
    app.processEvents()

    win.planner.alpha_edit.setText("0,1")
    win.planner.build_btn.click()
    app.processEvents()
    assert len(win.planner.points) == 2

    # pull a device offline → record_blockers non-empty
    win.manager.devices["daq"].disconnect()
    win.start_btn.click()
    app.processEvents()

    assert win.banner.isVisible()                   # red blocking banner
    assert "daq" in win.banner.text()
    assert not win.sweep_active                     # engine NOT started
    assert not list((tmp_path / "runs").rglob("*.h5"))
    assert "blockers" in win.console.toPlainText()

    # fix the cause → start now proceeds and the banner clears
    win.manager.devices["daq"].connect()
    win.manager.devices["daq"].start()
    win.start_btn.click()
    assert _spin(app, lambda: win.sweep_active, timeout_s=10)
    assert not win.banner.isVisible()
    assert _spin(app, lambda: not win.sweep_active, timeout_s=60)
    assert len(list((tmp_path / "runs").rglob("*.h5"))) == 2
