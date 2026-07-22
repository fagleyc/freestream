"""SSWT tunnel driver tests — register map, monitor, guarded control.

Runs on the plant simulator and a recording fake pymodbus client; no
hardware required.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tunnel_plc.config import TunnelConfig
from tunnel_plc.control import TunnelControl, WriteRefused
from tunnel_plc.emulator import SimGateway
from tunnel_plc.gateway import FakeClient, GatewayError, ModbusGateway
from tunnel_plc.monitor import TunnelMonitor
from tunnel_plc.registers import (BEARING_CAL, BEARING_TAGS, BLOCK1_ADDR,
                                  BLOCK1_ELEMENTS, BLOCK1_REGISTERS,
                                  BLOCK1_REGISTERS_EXT, BLOCK1_TAGS,
                                  BLOCK2_ADDR, decode_block1, decode_u32,
                                  encode_u32, element_addr, scale_bearing,
                                  unscale_bearing)


def _sim_config(**kw) -> TunnelConfig:
    return TunnelConfig(force_sim=True, poll_s=0.05, stale_after_s=0.5,
                        backoff_min_s=0.05, backoff_max_s=0.2,
                        button_hold_ms=120, **kw)


def _sim_monitor(cfg=None) -> TunnelMonitor:
    return TunnelMonitor(cfg or _sim_config())


# ── register map ─────────────────────────────────────────────────────────
def test_block_layout_matches_crimson_export():
    # Block1: 16 elements starting at protocol address 0 (element 1)
    assert BLOCK1_ADDR == element_addr(1) == 0
    assert BLOCK1_REGISTERS == 32
    assert [t for t, _a, _b in BLOCK1_TAGS] == [
        "RPM_Set", "Actual_RPM", "Tunnel_Fan_Stop_Button",
        "Tunnel_Fan_Start_Button", "Cooling_Fan_Start_Button",
        "Cooling_Fan_Stop_Button", "Bearing_Heater_On_Button",
        "Bearing_Temp_Low_Light", "Fan_Running_Light",
        "Console_Control_Light", "Oil_Level_Low_Light",
        "Inverter_Fault_Light", "Tunnel_Fan_Light_Start",
        "Tunnel_Fan_Light_Stop", "Cooling_Fan_Light_Start",
        "Cooling_Fan_Light_Stop"]
    # Block2: elements 101..105 → addresses 200..208
    assert BLOCK2_ADDR == {
        "Tunnel_Fan_Start_Button": element_addr(101),
        "Tunnel_Fan_Stop_Button": element_addr(102),
        "Cooling_Fan_Start_Button": element_addr(103),
        "Cooling_Fan_Stop_Button": element_addr(104),
        "RPM_Set": element_addr(105)}
    assert element_addr(101) == 200 and element_addr(105) == 208


def test_u32_word_order_roundtrip():
    for order in ("low_first", "high_first"):
        for v in (0, 1, 1234, 65535, 65536, 0x12345678, -1, -42):
            lo_hi = encode_u32(v, order)
            assert decode_u32(lo_hi[0], lo_hi[1], order) == v
    # a boolean 1: low word carries the bit
    assert encode_u32(1, "low_first") == (1, 0)
    assert encode_u32(1, "high_first") == (0, 1)
    with pytest.raises(ValueError):
        encode_u32(1, "sideways")


def test_decode_block1_scaling_and_booleans():
    values = [850, 848] + [0] * 14
    values[8] = 1                        # Fan_Running_Light
    regs = []
    for v in values:
        regs.extend(encode_u32(v, "low_first"))
    out = decode_block1(regs, "low_first", rpm_scale=1.0)
    assert out["rpm_set"] == 850 and out["actual_rpm"] == 848
    assert out["fan_running"] is True and out["inverter_fault"] is False
    out10 = decode_block1(regs, "low_first", rpm_scale=0.1)
    assert out10["rpm_set"] == pytest.approx(85.0)
    with pytest.raises(ValueError):
        decode_block1(regs[:10], "low_first")


# ── bearing temperatures (extended Block1, opt-in) ───────────────────────
def test_bearing_extension_layout():
    # elements 17/18/19 → protocol addresses 32/34/36; 38-register read
    assert [t for t, _a in BEARING_TAGS] == [
        "Analog_Feedback.B1", "Analog_Feedback.B2", "Analog_Feedback.B3"]
    assert BLOCK1_ELEMENTS == 16 and BLOCK1_REGISTERS == 32   # unchanged
    assert BLOCK1_REGISTERS_EXT == 38
    assert element_addr(17) == 32
    assert element_addr(19) == 36


def test_bearing_scaling_matches_csv_constants():
    # tunnel_tags.csv: B1 955–5035, B2 969–4979, B3 930–4994 → 0–150
    for attr, (raw_lo, raw_hi) in (("bearing_b1", (955, 5035)),
                                   ("bearing_b2", (969, 4979)),
                                   ("bearing_b3", (930, 4994))):
        cal = BEARING_CAL[attr]
        assert cal == (raw_lo, raw_hi, 0.0, 150.0)
        assert scale_bearing(raw_lo, cal) == pytest.approx(0.0)
        assert scale_bearing(raw_hi, cal) == pytest.approx(150.0)
        mid = (raw_lo + raw_hi) / 2.0
        assert scale_bearing(mid, cal) == pytest.approx(75.0)
        # round-trip through the emulator's inverse
        assert scale_bearing(unscale_bearing(85.0, cal), cal) == \
            pytest.approx(85.0, abs=0.05)


def test_decode_block1_extended_38_registers():
    values = [850, 848] + [0] * 14
    values += [unscale_bearing(85.0, BEARING_CAL["bearing_b1"]),
               unscale_bearing(90.0, BEARING_CAL["bearing_b2"]),
               unscale_bearing(75.0, BEARING_CAL["bearing_b3"])]
    regs = []
    for v in values:
        regs.extend(encode_u32(v, "low_first"))
    assert len(regs) == BLOCK1_REGISTERS_EXT
    out = decode_block1(regs, "low_first", rpm_scale=1.0)
    assert out["rpm_set"] == 850                     # base map intact
    assert out["bearing_b1"] == pytest.approx(85.0, abs=0.05)
    assert out["bearing_b2"] == pytest.approx(90.0, abs=0.05)
    assert out["bearing_b3"] == pytest.approx(75.0, abs=0.05)
    # 32-register default decode: NO bearing keys (snapshot stays None)
    out16 = decode_block1(regs[:BLOCK1_REGISTERS], "low_first")
    assert not any(k.startswith("bearing_b") for k in out16)


def test_monitor_bearing_temps_from_sim_extended_read():
    mon = _sim_monitor(_sim_config(bearing_temps=True))
    try:
        mon.connect()
        snap = mon.snapshot()
        assert len(snap.raw_registers) == 38         # ONE contiguous read
        for v in (snap.bearing_b1, snap.bearing_b2, snap.bearing_b3):
            assert v is not None and 70.0 < v < 100.0   # ~85 with drift
    finally:
        mon.disconnect()


def test_monitor_default_path_has_no_bearing_values():
    mon = _sim_monitor()                             # bearing_temps=False
    try:
        mon.connect()
        snap = mon.snapshot()
        assert len(snap.raw_registers) == 32         # default unchanged
        assert snap.bearing_b1 is None
        assert snap.bearing_b2 is None
        assert snap.bearing_b3 is None
    finally:
        mon.disconnect()


def test_bearing_config_roundtrip(tmp_path):
    cfg = TunnelConfig(bearing_temps=True, bearing_unit="°F")
    p = tmp_path / "bearing.json"
    cfg.save(p)
    back = TunnelConfig.load(p)
    assert back.bearing_temps is True
    assert back.bearing_unit == "°F"
    assert back.bearing_cal() == BEARING_CAL
    fresh = TunnelConfig()
    assert fresh.bearing_temps is False              # opt-in by default


# ── mocked pymodbus transport ────────────────────────────────────────────
def test_gateway_read_uses_one_contiguous_block():
    gw = ModbusGateway("10.0.0.1", unit_id=1, word_order="low_first")
    fake = FakeClient(read_map={0: [0] * 32})
    gw._client = fake
    regs = gw.read_registers(BLOCK1_ADDR, BLOCK1_REGISTERS)
    assert len(regs) == 32
    assert fake.calls == [("read", 0, 32, 1)]   # ONE atomic read, unit 1


def test_gateway_write_element_word_order():
    for order, expect in (("low_first", [850, 0]),
                          ("high_first", [0, 850])):
        gw = ModbusGateway("10.0.0.1", word_order=order)
        fake = FakeClient()
        gw._client = fake
        gw.write_element(208, 850)
        assert fake.calls == [("write", 208, expect, 1)]


def test_gateway_falls_back_to_fc6_when_fc16_rejected():
    gw = ModbusGateway("10.0.0.1", word_order="low_first")
    fake = FakeClient()
    fake.reject_fc16 = 1                     # ILLEGAL FUNCTION
    gw._client = fake
    gw.write_element(208, 850)
    assert fake.calls == [
        ("write", 208, [850, 0], 1),         # FC16 attempt
        ("write6", 208, 850, 1),             # FC6 low word
        ("write6", 209, 0, 1)]               # FC6 high word
    # working function remembered: next write skips FC16
    fake.calls.clear()
    gw.write_element(200, 1)
    assert fake.calls == [("write6", 200, 1, 1), ("write6", 201, 0, 1)]


def test_gateway_write_error_readable_when_all_functions_rejected():
    gw = ModbusGateway("10.0.0.1")
    fake = FakeClient()
    fake.reject_fc16 = 2                     # as the G315 said live
    fake.reject_fc6 = 2
    gw._client = fake
    with pytest.raises(GatewayError, match="ILLEGAL DATA ADDRESS"):
        gw.write_element(204, 1)


def test_pulse_failure_surfaces_as_write_refused_with_hint():
    cfg = _sim_config(rpm_max=1000.0)
    mon = TunnelMonitor(cfg, gateway=None)
    mon.connect()
    try:
        ctl = TunnelControl(cfg, mon, enable_writes=True)
        real_write = mon.gateway.write_element

        def rejecting(addr, value):
            raise GatewayError("write @204 = 1 failed: Modbus exception "
                               "2 on FC16: ILLEGAL DATA ADDRESS "
                               "(registers not readable/writable at "
                               "this address)")
        mon.gateway.write_element = rejecting
        with pytest.raises(WriteRefused, match="Crimson"):
            ctl.start_cooling_fan()
        mon.gateway.write_element = real_write
    finally:
        mon.disconnect()


def test_gateway_read_error_raises_after_retry():
    gw = ModbusGateway("10.0.0.1")
    fake = FakeClient()
    fake.fail_reads = True
    gw._client = fake
    with pytest.raises(GatewayError):
        gw.read_registers(0, 32)
    assert len(fake.calls) == 2          # one silent retry, then raise


# ── TunnelMonitor ────────────────────────────────────────────────────────
def test_monitor_snapshot_from_sim():
    mon = _sim_monitor()
    try:
        mon.connect()
        snap = mon.snapshot()
        assert not snap.stale and snap.age_s < 1.0
        assert snap.actual_rpm == 0 and not snap.fan_running
        assert snap.console_control            # sim default
        assert len(snap.raw_registers) == 32
    finally:
        mon.disconnect()


def test_monitor_has_no_write_capability():
    mon = _sim_monitor()
    writey = [n for n in dir(mon)
              if "write" in n.lower() or "command" in n.lower() or
              n.lower().startswith(("set_", "start_", "stop_tunnel"))]
    assert writey == [], f"monitor exposes write-ish API: {writey}"


def test_monitor_staleness_and_reconnect():
    mon = _sim_monitor()
    try:
        mon.connect()
        assert not mon.snapshot().stale
        mon.gateway.fail_comms = True          # kill comms
        time.sleep(0.8)                        # > stale_after_s (0.5)
        snap = mon.snapshot()
        assert snap.stale and snap.age_s > 0.5
        mon.gateway.fail_comms = False         # heal → backoff reconnect
        deadline = time.time() + 3.0
        while time.time() < deadline and mon.snapshot().stale:
            time.sleep(0.05)
        assert not mon.snapshot().stale, "monitor never recovered"
    finally:
        mon.disconnect()


def test_monitor_ring_records_history():
    mon = _sim_monitor()
    try:
        mon.connect()
        time.sleep(0.4)
        data = mon.ring.tail(100)
        assert data["t"].size >= 3
        assert (data["actual_rpm"] == 0).all()
    finally:
        mon.disconnect()


# ── TunnelControl ────────────────────────────────────────────────────────
def _armed(cfg=None):
    cfg = cfg or _sim_config(rpm_max=1000.0)
    mon = TunnelMonitor(cfg)
    mon.connect()
    ctl = TunnelControl(cfg, mon, enable_writes=True)
    return cfg, mon, ctl


def test_control_requires_explicit_enable():
    cfg = _sim_config(rpm_max=1000.0)
    mon = _sim_monitor(cfg)
    with pytest.raises(PermissionError):
        TunnelControl(cfg, mon)
    with pytest.raises(PermissionError):
        TunnelControl(cfg, mon, enable_writes=False)
    with pytest.raises(TypeError):       # keyword-only, no positional
        TunnelControl(cfg, mon, True)    # noqa: E501  pylint: disable=too-many-function-args


def test_rpm_refused_until_rpm_max_configured():
    cfg, mon, ctl = _armed(_sim_config())          # rpm_max = 0 default
    try:
        with pytest.raises(WriteRefused, match="rpm_max"):
            ctl.set_rpm(100)
        assert mon.gateway.write_history == []
    finally:
        mon.disconnect()


def test_rpm_clamped_to_max_and_floor():
    cfg, mon, ctl = _armed()
    try:
        assert ctl.set_rpm(5000) == 1000.0         # clamped to rpm_max
        assert ctl.set_rpm(-50) == 0.0             # floored at 0
        writes = [(a, v) for (_t, a, v) in mon.gateway.write_history]
        # raw register = RPM / rpm_scale (Crimson fixed-point ×10:
        # live register 600 == HMI display 60.0)
        assert cfg.rpm_scale == 0.1
        assert writes == [(BLOCK2_ADDR["RPM_Set"], 10000),
                          (BLOCK2_ADDR["RPM_Set"], 0)]
    finally:
        mon.disconnect()


def test_writes_refused_on_inverter_fault_but_stop_allowed():
    cfg, mon, ctl = _armed()
    try:
        mon.gateway.set_fault(True)
        time.sleep(0.15)                           # next poll sees it
        assert mon.snapshot().inverter_fault
        with pytest.raises(WriteRefused, match="[Ff]ault"):
            ctl.set_rpm(100)
        with pytest.raises(WriteRefused, match="[Ff]ault"):
            ctl.start_tunnel_fan()
        ctl.stop_tunnel_fan()                      # safe direction: allowed
        stops = [(a, v) for (_t, a, v) in mon.gateway.write_history
                 if a == BLOCK2_ADDR["Tunnel_Fan_Stop_Button"]]
        assert stops == [(202, 1), (202, 0)]
    finally:
        mon.disconnect()


def test_writes_refused_when_snapshot_stale():
    cfg, mon, ctl = _armed()
    try:
        mon.disconnect()                           # polling stops
        time.sleep(0.7)                            # > stale_after_s
        mon.gateway.connect()                      # transport up, data old
        assert mon.snapshot().stale
        with pytest.raises(WriteRefused, match="STALE"):
            ctl.set_rpm(100)
        with pytest.raises(WriteRefused, match="STALE"):
            ctl.start_cooling_fan()
    finally:
        mon.disconnect()


def test_momentary_pulse_writes_1_then_0_with_hold():
    cfg, mon, ctl = _armed()
    try:
        ctl.start_tunnel_fan()
        hist = [(t, a, v) for (t, a, v) in mon.gateway.write_history
                if a == BLOCK2_ADDR["Tunnel_Fan_Start_Button"]]
        assert [(a, v) for (_t, a, v) in hist] == [(200, 1), (200, 0)]
        held = hist[1][0] - hist[0][0]
        assert held >= cfg.button_hold_ms / 1000.0 * 0.8, \
            f"pulse held only {held * 1000:.0f}ms"
        # the sim plant reacts: fan spins up toward the setpoint
        ctl.set_rpm(400)
        deadline = time.time() + 3.0
        while time.time() < deadline and \
                mon.snapshot().actual_rpm < 50:
            time.sleep(0.05)
        snap = mon.snapshot()
        assert snap.fan_running and snap.actual_rpm > 50
    finally:
        mon.disconnect()


def test_every_write_is_logged():
    cfg, mon, ctl = _armed()
    try:
        ctl.set_rpm(200)
        ctl.start_tunnel_fan()
        ctl.stop_tunnel_fan()
        tags = [r.tag for r in ctl.write_log]
        assert tags == ["RPM_Set", "Tunnel_Fan_Start_Button",
                        "Tunnel_Fan_Stop_Button"]
        rpm_rec = ctl.write_log[0]
        assert rpm_rec.old == 0 and rpm_rec.new == 200.0
        assert time.time() - rpm_rec.t < 10
        # momentary TODO reminder present until verified
        assert "UNVERIFIED" in ctl.write_log[1].note
    finally:
        mon.disconnect()


def test_control_shares_monitor_gateway():
    cfg, mon, ctl = _armed()
    try:
        assert ctl.gateway is mon.gateway
    finally:
        mon.disconnect()


# ── config ───────────────────────────────────────────────────────────────
def test_config_roundtrip(tmp_path):
    cfg = TunnelConfig(rpm_max=900, word_order="high_first",
                       word_order_verified=True, poll_s=1.0)
    p = tmp_path / "tunnel.json"
    cfg.save(p)
    back = TunnelConfig.load(p)
    assert back.ip == "192.168.1.50" and back.port == 502
    assert back.unit_id == 1
    assert back.rpm_max == 900
    assert back.word_order == "high_first" and back.word_order_verified
    assert back.poll_s == 1.0
    # safety defaults
    fresh = TunnelConfig()
    assert fresh.rpm_max == 0.0                 # writes refused by default
    assert fresh.word_order == "low_first"      # live-verified 2026-07-07
    assert fresh.word_order_verified is True
    assert fresh.rpm_scale == 0.1               # reg 600 == HMI 60.0, live
    assert fresh.momentary_verified is False    # still needs physical test
    with pytest.raises(ValueError):
        TunnelConfig(word_order="banana")


def _run_all():
    import inspect
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        kwargs = {}
        if "tmp_path" in inspect.signature(fn).parameters:
            import tempfile
            with tempfile.TemporaryDirectory() as d:
                fn(Path(d))
                print(f"  PASS {fn.__name__}")
                continue
        fn(**kwargs)
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} tunnel tests passed.")


if __name__ == "__main__":
    _run_all()
