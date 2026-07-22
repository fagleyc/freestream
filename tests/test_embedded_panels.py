"""Embedded standalone device panels — one shared device instance ever.

The crescent established the pattern (see test_crescent_device.py); these
tests hold the other five devices to it. For each device the DeviceConfig
dialog embeds the devices-app panel wired to the adapter's OWN driver
instance (``panel.device/monitor IS adapter.driver`` — never a second
connection), with the Connection row hidden because Freestream owns the
lifecycle, and the panel coming alive when the ADAPTER connects (no
Connect click inside the panel).

Per-device representative interactions (sim):

* tunnel  — the panel's ARM flow (safety intact: confirm dialog, rpm_max
            gate) builds a TunnelControl against the SHARED monitor and
            Apply RPM lands in the PLC snapshot;
* traverse — manifest-disabled by default; enabled via a temp manifest,
            a calibrated Move through the panel moves the adapter's axis;
* strainbook — live bridge tiles show values from the shared ring
            (Forces load-limit monitor runs on the same refresh);
* daqbook — streaming tiles show values from the shared ring;
* ate     — the panel CHAINS the device's single-slot callbacks (the
            adapter's hooks keep firing), a panel Move drives alpha, and
            detach() hands the callbacks back on dialog close.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication, QMessageBox      # noqa: E402

from freestream.manager import DeviceManager               # noqa: E402
from freestream.adapters.tunnel import TunnelAdapter       # noqa: E402
from freestream.adapters.strainbook import StrainbookAdapter  # noqa: E402
from freestream.adapters.daqbook import DaqbookAdapter     # noqa: E402
from freestream.adapters.ate import AteBalanceAdapter      # noqa: E402
from freestream.app.device_config import DeviceConfigDialog  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


def _wait(cond, timeout=20.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _teardown(dlg):
    dlg._pump.stop()
    dlg._stop_device_panel()


def _tile_texts(tiles):
    return [t.value.text() for t in tiles._tiles.values()]


# ── tunnel ────────────────────────────────────────────────────────────────
@pytest.fixture()
def tunnel():
    a = TunnelAdapter(sim=True)
    yield a
    a.disconnect()


def test_tunnel_panel_shares_monitor_and_reflects_connect(app, tunnel):
    dlg = DeviceConfigDialog(tunnel)
    try:
        panel = dlg._device_panel
        assert panel is not None, "tunnel dialog lacks the device panel"
        assert panel.monitor is tunnel.driver          # ONE monitor ever
        assert panel.conn_group.isHidden()
        assert not panel.arm_btn.isEnabled()           # disconnected

        tunnel.connect()                               # Freestream connects
        assert _wait(lambda: tunnel.connected, 5.0)
        panel._refresh_ui()                            # UI-timer tick
        assert panel.arm_btn.isEnabled(), \
            "panel did not come alive on the adapter's connect"
        assert "SIM" in panel.lamp.text()
    finally:
        _teardown(dlg)


def test_tunnel_panel_arm_and_set_rpm(app, tunnel, monkeypatch):
    """Panel ARM (with the safety confirm) + Apply RPM through the SHARED
    monitor; disconnecting the adapter DISARMS the panel."""
    monkeypatch.setattr(
        QMessageBox, "question",
        lambda *a, **k: QMessageBox.StandardButton.Yes)
    tunnel.connect()
    assert _wait(lambda: tunnel.connected, 5.0)
    dlg = DeviceConfigDialog(tunnel)
    try:
        panel = dlg._device_panel
        panel._refresh_ui()
        assert panel.control is None                   # no write path yet
        panel.arm_btn.setChecked(True)
        panel._handle_arm()
        assert panel.control is not None, "arming failed"
        # the write path wraps the SHARED monitor (rpm_max still enforced)
        assert panel.control.monitor is tunnel.driver
        assert panel.rpm_spin.maximum() == tunnel.config.rpm_max

        panel.rpm_spin.setValue(400.0)
        panel._apply_rpm()
        assert _wait(lambda: abs(tunnel.snapshot().rpm_set - 400.0) < 1.0,
                     10.0), "panel RPM setpoint never reached the PLC"
        assert len(panel.control.write_log) >= 1

        tunnel.disconnect()                            # host disconnects…
        panel._refresh_ui()
        assert panel.control is None, "disconnect did not disarm writes"
        assert not panel.arm_btn.isChecked()
    finally:
        _teardown(dlg)


# ── traverse (manifest-disabled by default) ───────────────────────────────
@pytest.fixture()
def traverse(tmp_path):
    """Adapter built through DeviceManager with traverse ENABLED via a
    temp manifest (it ships disabled)."""
    src = json.loads((Path(__file__).resolve().parents[1] / "freestream" /
                      "devices_manifest.json").read_text(encoding="utf-8"))
    # traverse now ships enabled (Mode 3 uses it); here we add it to
    # mode1 via a temp manifest so the embedded panel can be exercised
    # in a mode1 context.
    src["devices"]["traverse"]["enabled"] = True
    src["modes"]["mode1"]["traverse"] = "traverse"
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(src), encoding="utf-8")
    mgr = DeviceManager("mode1", sim=True, manifest_path=path)
    yield mgr.devices["traverse"]
    mgr.disconnect_all()


def test_traverse_panel_shares_drive_and_moves(app, traverse):
    traverse.connect()
    assert _wait(lambda: traverse.connected, 5.0)
    dlg = DeviceConfigDialog(traverse)
    try:
        panel = dlg._device_panel
        assert panel is not None, "traverse dialog lacks the device panel"
        assert panel.device is traverse.driver         # ONE drive ever
        assert panel.conn_group.isHidden()
        # the full standalone GUI: X/Y/Z cards + Diagnostics + Calibration
        assert set(panel.cards) == {"X", "Y", "Z"}
        assert panel.diag_panel is not None
        assert panel.cal_panel is not None

        panel._refresh_ui()                            # sync connected UI
        assert panel.estop_btn.isEnabled()
        card = panel.cards["X"]
        assert card.stop_btn.isEnabled()
        assert card.move_btn.isEnabled()               # sim axes calibrated

        # a calibrated Move through the panel moves the adapter's axis
        c0 = traverse.driver.state()["X"]["counts"]
        card.target.setValue(2.0)
        card.move_btn.click()
        assert _wait(lambda: not traverse.driver.state()["X"]["moving"]
                     and traverse.driver.state()["X"]["counts"] != c0, 10.0), \
            "panel Move did not move the shared drive"
    finally:
        _teardown(dlg)


def test_traverse_detach_restores_drive_callbacks(app, traverse):
    drv = traverse.driver
    before = (drv.on_status, drv.on_module_status)
    dlg = DeviceConfigDialog(traverse)
    panel = dlg._device_panel
    assert drv.on_module_status is not None     # panel wired the diag log
    _teardown(dlg)                              # dialog closes → detach()
    assert (drv.on_status, drv.on_module_status) == before, \
        "closed dialog left callbacks pointing into the dead panel"
    assert panel is not None


# ── strainbook ────────────────────────────────────────────────────────────
@pytest.fixture()
def strainbook():
    a = StrainbookAdapter(sim=True)
    yield a
    a.disconnect()


def test_strainbook_panel_shares_device_and_shows_live(app, strainbook):
    dlg = DeviceConfigDialog(strainbook)
    try:
        panel = dlg._device_panel
        assert panel is not None, "strainbook dialog lacks the panel"
        assert panel.device is strainbook.driver       # ONE device ever
        assert panel.conn_group.isHidden()
        # Forces (load-limit alarm monitor) + Channels ride along
        assert panel.forces_panel is not None
        assert panel.channels_panel is not None

        strainbook.connect()                           # Freestream connects
        strainbook.start()
        assert _wait(lambda: strainbook.driver.frame_count() > 10, 10.0)
        panel._refresh_ui()                            # binds channels
        panel._refresh_ui()                            # then paints tiles
        assert panel.tare_btn.isEnabled(), \
            "panel did not come alive on the adapter's connect"
        texts = _tile_texts(panel.tiles)
        assert texts and any(any(ch.isdigit() for ch in t) for t in texts), \
            f"no live bridge values in the tiles: {texts}"
    finally:
        _teardown(dlg)


def test_strainbook_balance_config_propagates_to_forces_page(app):
    """The propagation fix: changing the balance layout in the EMBEDDED
    StrainBook device panel reaches the Freestream Forces page (single
    source of truth = the shared adapter's balance_config), and a
    sim-acquired /StrainBook_0 drain carries the matching channel names —
    all WITHOUT touching main_window."""
    from freestream.manager import DeviceManager
    from freestream.config import FreestreamConfig
    from freestream.app.forces import ForcesPanel

    mgr = DeviceManager("mode1", sim=True)
    balance = mgr.by_role("balance")
    assert isinstance(balance, StrainbookAdapter)
    assert balance.balance_config == "Force"            # default everywhere

    balance.connect()
    balance.start()
    assert _wait(lambda: balance.driver.frame_count() > 10, 10.0)

    cfg = FreestreamConfig()
    assert cfg.balance_config == "Force"
    forces_page = ForcesPanel(mgr, cfg)                  # the Freestream page
    dlg = DeviceConfigDialog(balance)                    # embeds the device panel
    try:
        device_panel = dlg._device_panel
        assert device_panel is not None
        assert device_panel.device is balance.driver     # ONE device ever

        # operator flips the layout in the EMBEDDED device panel's Forces tab
        device_panel.forces_panel.bal_config.setCurrentText("Moment")

        # single source of truth updated on the shared adapter…
        assert balance.balance_config == "Moment"
        # …the recorded channel names follow (recorder reads channels())
        names = [c.name for c in balance.channels()]
        assert names[:4] == ["AftPitch", "AftYaw", "FwdPitch", "FwdYaw"]
        assert "N1" not in names

        # the Freestream Forces page inherits Moment on its refresh timer
        # (read-only label — the device panel is the single editor)
        forces_page._sample()
        assert forces_page.layout_lbl.text() == "Moment"
        assert forces_page._layout == "Moment"
        assert cfg.balance_config == "Moment"

        # a sim-acquired /StrainBook_0 block uses the moment names
        assert _wait(lambda: balance.driver.frame_count() > 0, 5.0)
        time.sleep(0.2)
        block = balance.drain_block()
        for n in ("AftPitch", "AftYaw", "FwdPitch", "FwdYaw"):
            assert n in block
        assert "N1" not in block
    finally:
        forces_page.shutdown()
        _teardown(dlg)
        balance.disconnect()
        mgr.disconnect_all()


# ── daqbook ───────────────────────────────────────────────────────────────
@pytest.fixture()
def daqbook():
    a = DaqbookAdapter(sim=True)
    yield a
    a.disconnect()


def test_daqbook_panel_shares_device_and_streams(app, daqbook):
    daqbook.connect()
    daqbook.start()
    assert _wait(lambda: daqbook.driver.frame_count() > 10, 10.0)
    dlg = DeviceConfigDialog(daqbook)
    try:
        panel = dlg._device_panel
        assert panel is not None, "daqbook dialog lacks the panel"
        assert panel.device is daqbook.driver          # ONE device ever
        assert panel.conn_group.isHidden()
        assert panel.channels_panel is not None

        # constructed against an already-streaming host → tiles bound at
        # init; a refresh paints live values from the SHARED ring
        panel._refresh_ui()
        texts = _tile_texts(panel.tiles)
        assert texts and any(any(ch.isdigit() for ch in t) for t in texts), \
            f"no streaming values in the tiles: {texts}"

        n0 = daqbook.driver.frame_count()
        assert _wait(lambda: daqbook.driver.frame_count() > n0, 5.0), \
            "stream stalled with the panel attached"
    finally:
        _teardown(dlg)


# ── ate ───────────────────────────────────────────────────────────────────
@pytest.fixture()
def ate():
    a = AteBalanceAdapter(sim=True)
    yield a
    a.disconnect()


def test_ate_panel_chains_callbacks_and_moves(app, ate):
    ate.connect()
    ate.start()
    assert _wait(lambda: ate.connected, 5.0)
    dlg = DeviceConfigDialog(ate)
    try:
        panel = dlg._device_panel
        assert panel is not None, "ate dialog lacks the device panel"
        assert panel.device is ate.driver              # ONE TMS client ever
        assert panel.connect_panel.isHidden()
        # suite owns acquisition timing → the OGI average duration spin is
        # locked in the embedded panel
        assert not panel.run_panel.sample_secs.isEnabled()
        assert "Measurement Setup" in panel.run_panel.sample_secs.toolTip()

        # frames reach BOTH sinks: the panel's ring (live tab) and the
        # adapter's accumulator (recorder) — single-slot callback CHAINED
        assert _wait(lambda: panel.ring.count > 0, 10.0), \
            "no frames reached the embedded panel's ring"
        assert _wait(lambda: bool(ate.latest()), 5.0), \
            "chaining broke the adapter's own frame hook"

        # a Move through the panel's motion tab drives the shared device;
        # the adapter's position cache follows via the chained on_reply
        panel.motion_panel._inc_spin.setValue(5.0)
        from PyQt6.QtWidgets import QPushButton
        inc_box = panel.motion_panel._inc_spin.parent()
        move_btn = [b for b in inc_box.findChildren(QPushButton)
                    if b.text() == "Move"][0]
        move_btn.click()
        assert _wait(lambda: abs(ate.positions()["alpha"] - 5.0) < 0.1,
                     20.0), "panel move never reached alpha=5"
    finally:
        _teardown(dlg)


def test_ate_detach_restores_adapter_callbacks(app, ate):
    dev = ate.driver
    before = (dev.on_status, dev.on_reply, dev.on_frame)
    assert before[2] is not None                   # adapter's frame hook
    dlg = DeviceConfigDialog(ate)
    assert dev.on_frame is not before[2]           # panel chained it
    _teardown(dlg)                                 # dialog closes → detach()
    assert (dev.on_status, dev.on_reply, dev.on_frame) == before, \
        "closed dialog left chained callbacks in place"
