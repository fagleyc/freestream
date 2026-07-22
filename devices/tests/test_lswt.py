"""LSWT fan-drive tests — calibration, config, ACS530 protocol, ramped
sim drive, offscreen GUI smoke.

Runs on the fan simulator / a recording fake Modbus client; NEVER
touches hardware. Protocol facts trace to the deployed C#
``Tool_LSWT_Flow_Velocity\\HwControllerVelocityLSWT_ACB530.cs``.
"""

import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lswt import calibration
from lswt.config import LswtConfig, defaults_path, load_startup_config
from lswt.device import LswtDrive
from lswt.drive import (CMD_START, CMD_STOP, REG_ACTUAL_HZ_X10,
                        REG_CONTROL, REG_REFERENCE, AbbAcs530,
                        reference_counts)


def _wait(cond, timeout=10.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _sim_config(**kw) -> LswtConfig:
    kw.setdefault("force_sim", True)
    kw.setdefault("poll_s", 0.02)
    kw.setdefault("sim_tau_s", 0.15)
    return LswtConfig.for_tunnel(kw.pop("tunnel", "north"), **kw)


# ── calibration ──────────────────────────────────────────────────────────
def test_calibration_table_length_and_endpoints():
    """61-point measured table (C# lines 59–65): 0 Hz → 0 ft/s,
    60 Hz → 105.6851 ft/s, strictly monotonic."""
    assert calibration.FPS_AT_HZ.shape == (61,)
    assert calibration.FPS_AT_HZ[0] == 0.0
    assert calibration.FPS_AT_HZ[60] == pytest.approx(105.6851)
    assert np.all(np.diff(calibration.FPS_AT_HZ) > 0)


def test_calibration_known_points_verbatim():
    """Spot-check values copied exactly from the C# table."""
    assert calibration.FPS_AT_HZ[1] == pytest.approx(1.758977)
    assert calibration.FPS_AT_HZ[30] == pytest.approx(52.53426)
    assert calibration.FPS_AT_HZ[59] == pytest.approx(103.4661)


def test_hz_fps_roundtrip():
    for hz in (0.0, 7.3, 15.0, 29.9, 42.5, 60.0):
        fps = calibration.hz_to_fps(hz)
        assert calibration.fps_to_hz(fps) == pytest.approx(hz, abs=1e-9)


def test_calibration_clamps_at_ends():
    assert calibration.hz_to_fps(-5.0) == 0.0
    assert calibration.hz_to_fps(100.0) == pytest.approx(105.6851)
    assert calibration.fps_to_hz(-1.0) == 0.0
    assert calibration.fps_to_hz(500.0) == pytest.approx(60.0)


def test_unit_conversions():
    """Physical factors for mps/kph/mph (match the C# maxima to <0.01%);
    Mach via the C# SpeedUnitsConversion ratio."""
    fps = calibration.MAX_FPS
    assert calibration.fps_to_unit(fps, "mps") == \
        pytest.approx(calibration.UNIT_MAXIMA["mps"], rel=1e-4)
    assert calibration.fps_to_unit(fps, "kph") == \
        pytest.approx(calibration.UNIT_MAXIMA["kph"], rel=1e-4)
    assert calibration.fps_to_unit(fps, "mph") == \
        pytest.approx(calibration.UNIT_MAXIMA["mph"], rel=1e-3)
    assert calibration.fps_to_unit(fps, "Mach") == \
        pytest.approx(calibration.UNIT_MAXIMA["Mach"], rel=1e-9)
    # round-trips
    for unit in calibration.UNITS:
        v = calibration.fps_to_unit(50.0, unit)
        assert calibration.unit_to_fps(v, unit) == pytest.approx(50.0)
    with pytest.raises(ValueError):
        calibration.fps_to_unit(1.0, "knots")


# ── config ───────────────────────────────────────────────────────────────
def test_config_json_roundtrip(tmp_path):
    cfg = LswtConfig.for_tunnel("south", ramp_hz_per_s=3.5, max_hz=45.0,
                                ip="10.0.0.7")
    p = tmp_path / "cfg.json"
    cfg.save(p)
    back = LswtConfig.load(p)
    assert back.to_dict() == cfg.to_dict()
    assert back.tunnel == "south"
    assert back.label == "South LSWT"
    assert back.reference_sign == -1


def test_config_per_tunnel_defaults():
    north = LswtConfig.for_tunnel("north")
    south = LswtConfig.for_tunnel("south")
    assert north.label == "North LSWT"
    assert south.label == "South LSWT"
    assert north.unit_id == 1 and south.unit_id == 1
    assert north.port == 502
    with pytest.raises(ValueError):
        LswtConfig.for_tunnel("east")
    with pytest.raises(ValueError):
        LswtConfig(reference_sign=2)


def test_defaults_path_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("LSWT_DEFAULTS", str(tmp_path))
    assert defaults_path("north") == tmp_path / "defaults_north.json"
    assert defaults_path("south") == tmp_path / "defaults_south.json"
    # save + startup auto-load per tunnel
    cfg = LswtConfig.for_tunnel("north", ip="10.1.2.3")
    defaults_path("north").parent.mkdir(parents=True, exist_ok=True)
    cfg.save(defaults_path("north"))
    loaded = load_startup_config("north")
    assert loaded.ip == "10.1.2.3"
    # south has no defaults file → factory defaults
    assert load_startup_config("south").ip == "192.168.0.1"


def test_load_startup_config_corrupt_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("LSWT_DEFAULTS", str(tmp_path))
    defaults_path("north").write_text("{not json", encoding="utf-8")
    assert load_startup_config("north").label == "North LSWT"


# ── ACS530 protocol against a recording fake client ─────────────────────
class _FakeResult:
    def __init__(self, registers=None, error=False):
        self.registers = registers or []
        self._error = error

    def isError(self):
        return self._error


class _FakeClient:
    """Records writes; serves canned reads. pymodbus 3.x surface."""

    def __init__(self):
        self.writes = []                 # (address, raw u16 value)
        self.read_registers = {REG_ACTUAL_HZ_X10: [452]}

    def write_register(self, address, value=None, **kw):
        self.writes.append((address, value))
        return _FakeResult()

    def read_holding_registers(self, address, count=1, **kw):
        return _FakeResult(self.read_registers.get(address,
                                                   [0] * count))

    def close(self):
        pass


def _fake_drive(sign=-1):
    d = AbbAcs530("192.0.2.1", reference_sign=sign)
    d._client = _FakeClient()            # bypass connect — no hardware
    return d, d._client


def test_protocol_start_stop_words():
    """Start writes 1151 to wire 0; stop writes 1150 (C# lines 233,
    237–238 — FieldTalk address 1 = wire 0)."""
    d, fake = _fake_drive()
    d.start()
    d.stop()
    assert fake.writes == [(REG_CONTROL, CMD_START),
                           (REG_CONTROL, CMD_STOP)]
    assert REG_CONTROL == 0 and CMD_STOP == 1150 and CMD_START == 1151


def test_protocol_reference_negative_sign_and_scaling():
    """30 Hz → 10000 counts → wire value −10000 (C# line 191 wrote the
    NEGATIVE of the scaled value; FieldTalk 2 = wire 1)."""
    assert REG_REFERENCE == 1
    assert reference_counts(30.0) == 10000
    assert reference_counts(60.0) == 20000
    assert reference_counts(0.0) == 0
    assert reference_counts(75.0) == 20000       # clamped
    d, fake = _fake_drive(sign=-1)
    d.write_reference(reference_counts(30.0))
    addr, raw = fake.writes[-1]
    assert addr == REG_REFERENCE
    assert raw == (-10000) & 0xFFFF              # 55536 on the wire
    assert raw - 0x1_0000 == -10000
    # out-of-range counts clamp to full scale, sign still applied
    d.write_reference(25000)
    assert fake.writes[-1][1] == (-20000) & 0xFFFF


def test_protocol_reference_positive_sign():
    d, fake = _fake_drive(sign=1)
    d.write_reference(10000)
    assert fake.writes[-1] == (REG_REFERENCE, 10000)


def test_protocol_actual_hz_decode():
    """Wire 102 (FieldTalk 103) is output frequency × 10: 452 → 45.2 Hz;
    signed decode for the negative-reference convention."""
    assert REG_ACTUAL_HZ_X10 == 102
    d, fake = _fake_drive()
    assert d.read_actual_hz() == pytest.approx(45.2)
    fake.read_registers[REG_ACTUAL_HZ_X10] = [(-452) & 0xFFFF]
    assert d.read_actual_hz() == pytest.approx(-45.2)


# ── drive sim (ramp, velocity, estop, staleness) ────────────────────────
def test_sim_ramp_obeys_rate_and_never_jumps():
    cfg = _sim_config(ramp_hz_per_s=5.0)
    dev = LswtDrive(cfg)
    try:
        dev.connect()
        dev.fan_start()
        dev.set_hz(30.0)
        assert _wait(lambda: dev.state()["cmd_hz"] > 2.0, 5.0)
        time.sleep(0.5)
        data = dev.ring.tail(10_000)
        t, cmd = data["t"], data["cmd_hz"]
        assert t.size >= 10
        dts = np.diff(t)
        dcmd = np.diff(cmd)
        # commanded reference never steps faster than the ramp rate
        assert np.all(dcmd <= cfg.ramp_hz_per_s * dts + 0.15), \
            f"ramp jumped: max {dcmd.max():.3f} Hz in {dts.max():.3f} s"
        assert np.all(dcmd >= -0.001)            # monotonic toward target
        # overall bound: cmd growth ≤ ramp × elapsed
        assert cmd[-1] - cmd[0] <= \
            cfg.ramp_hz_per_s * (t[-1] - t[0]) + 0.2
    finally:
        dev.disconnect()


def test_sim_ramp_reaches_setpoint_and_fan_spins():
    cfg = _sim_config(ramp_hz_per_s=50.0)
    dev = LswtDrive(cfg)
    try:
        dev.connect()
        dev.fan_start()
        dev.set_hz(20.0)
        assert _wait(lambda: dev.state()["cmd_hz"] ==
                     pytest.approx(20.0, abs=0.01), 5.0)
        assert _wait(lambda: dev.state()["actual_hz"] > 15.0, 5.0)
        st = dev.state()
        assert st["velocity_fps"] == pytest.approx(
            calibration.hz_to_fps(st["actual_hz"]), abs=1e-6)
    finally:
        dev.disconnect()


def test_sim_setpoint_clamped_to_max_hz():
    cfg = _sim_config(max_hz=40.0)
    dev = LswtDrive(cfg)
    try:
        dev.connect()
        dev.set_hz(55.0)
        assert dev.state()["setpoint_hz"] == pytest.approx(40.0)
    finally:
        dev.disconnect()


def test_sim_set_velocity_roundtrips_through_calibration():
    dev = LswtDrive(_sim_config())
    try:
        dev.connect()
        dev.set_velocity(50.0)
        st = dev.state()
        assert st["setpoint_hz"] == pytest.approx(
            calibration.fps_to_hz(50.0), abs=1e-9)
        assert calibration.hz_to_fps(st["setpoint_hz"]) == \
            pytest.approx(50.0, abs=1e-6)
    finally:
        dev.disconnect()


def test_sim_estop_stops_and_zeroes_immediately():
    cfg = _sim_config(ramp_hz_per_s=50.0)
    dev = LswtDrive(cfg)
    try:
        dev.connect()
        dev.fan_start()
        dev.set_hz(25.0)
        assert _wait(lambda: dev.state()["cmd_hz"] > 5.0, 5.0)
        dev.estop()
        st = dev.state()                 # immediately, no waiting
        assert st["running"] is False
        assert st["setpoint_hz"] == 0.0
        assert st["cmd_hz"] == 0.0
        assert dev._drive.last_control == CMD_STOP
        assert dev._drive.last_reference == 0
    finally:
        dev.disconnect()


def test_sim_fan_stop_writes_stop_word_and_zero_reference():
    dev = LswtDrive(_sim_config(ramp_hz_per_s=50.0))
    try:
        dev.connect()
        dev.fan_start()
        assert dev._drive.last_control == CMD_START
        dev.set_hz(10.0)
        assert _wait(lambda: dev.state()["cmd_hz"] > 1.0, 5.0)
        dev.fan_stop()
        assert dev._drive.last_control == CMD_STOP
        assert dev._drive.last_reference == 0
        assert dev.state()["running"] is False
    finally:
        dev.disconnect()


def test_sim_staleness_alerts_but_never_autostops():
    cfg = _sim_config(stale_after_s=0.3)
    dev = LswtDrive(cfg)
    msgs = []
    dev.on_status = msgs.append
    try:
        dev.connect()
        dev.fan_start()
        dev.set_hz(10.0)
        assert dev.state()["stale"] is False
        dev._drive.fail_reads = True     # simulated comm loss
        assert _wait(lambda: dev.state()["stale"], 5.0)
        st = dev.state()
        assert st["running"] is True     # deliberately NOT auto-stopped
        assert dev._drive.last_control == CMD_START
        assert any("poll error" in m for m in msgs)
        # recovery clears staleness
        dev._drive.fail_reads = False
        assert _wait(lambda: not dev.state()["stale"], 5.0)
    finally:
        dev.disconnect()


# ── GUI offscreen smoke ──────────────────────────────────────────────────
@pytest.fixture(scope="module")
def qapp():
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_gui_builds_for_both_tunnels(qapp):
    from lswt.app.main_window import LswtMainWindow
    for tunnel, title in (("north", "North LSWT — Fan Control"),
                          ("south", "South LSWT — Fan Control")):
        win = LswtMainWindow(_sim_config(tunnel=tunnel))
        try:
            assert win.windowTitle() == title
            assert win.panel.config.tunnel == tunnel
        finally:
            win.panel.shutdown()
            win.close()


def test_gui_arm_gating_and_estop_always_live(qapp):
    from lswt.app.main_window import LswtMainWindow
    win = LswtMainWindow(_sim_config())
    panel = win.panel
    try:
        # E-STOP live even before connecting
        assert panel.estop_btn.isEnabled()
        assert not panel.arm_btn.isEnabled()
        panel._handle_connect()
        assert panel.device.connected and panel.device.sim_mode
        # connected but DISARMED: start/stop/apply locked, E-STOP live
        assert panel.arm_btn.isEnabled()
        assert not panel.start_btn.isEnabled()
        assert not panel.stop_btn.isEnabled()
        assert not panel.apply_btn.isEnabled()
        assert panel.estop_btn.isEnabled()
        # arm (sim: no confirm dialog)
        panel.arm_btn.setChecked(True)
        panel._handle_arm()
        assert panel.start_btn.isEnabled()
        assert panel.stop_btn.isEnabled()
        assert panel.apply_btn.isEnabled()
        assert panel.estop_btn.isEnabled()
        # start the fan through the armed path
        panel.hz_spin.setValue(15.0)
        panel._start_fan()
        assert panel.device.state()["running"] is True
        # disarm relocks the commands; E-STOP still live
        panel.arm_btn.setChecked(False)
        panel._handle_arm()
        assert not panel.start_btn.isEnabled()
        assert panel.estop_btn.isEnabled()
        panel.device_estop()
        assert panel.device.state()["running"] is False
    finally:
        panel.shutdown()
        win.close()


def test_gui_setpoint_spins_cross_update(qapp):
    from lswt.app.main_window import LswtMainWindow
    win = LswtMainWindow(_sim_config())
    panel = win.panel
    try:
        assert panel.unit_combo.currentText() == "fps"
        panel.hz_spin.setValue(30.0)
        assert panel.vel_spin.value() == pytest.approx(
            calibration.hz_to_fps(30.0), abs=0.01)
        panel.vel_spin.setValue(50.0)
        assert panel.hz_spin.value() == pytest.approx(
            calibration.fps_to_hz(50.0), abs=0.1)
        # unit switch re-expresses the velocity without moving the Hz
        hz_before = panel.hz_spin.value()
        panel.unit_combo.setCurrentText("mps")
        assert panel.hz_spin.value() == pytest.approx(hz_before, abs=0.01)
        assert panel.vel_spin.value() == pytest.approx(
            calibration.fps_to_unit(calibration.hz_to_fps(hz_before),
                                    "mps"), abs=0.05)
    finally:
        panel.shutdown()
        win.close()


def test_gui_refresh_and_ring_plot_path(qapp):
    """Connected sim panel refreshes its dashboard without error and the
    ring carries samples for the strip charts."""
    from lswt.app.main_window import LswtMainWindow
    win = LswtMainWindow(_sim_config(ramp_hz_per_s=50.0))
    panel = win.panel
    try:
        panel._handle_connect()
        panel.arm_btn.setChecked(True)
        panel._handle_arm()
        panel.hz_spin.setValue(20.0)
        panel._start_fan()
        assert _wait(lambda: panel.device.ring.count > 10, 5.0)
        panel._refresh_ui()
        assert "Hz" in panel.ramp_lbl.text() or \
            panel.ramp_lbl.text() == ""
        assert panel.tile_hz.value.text() != "--"
    finally:
        panel.shutdown()
        win.close()
