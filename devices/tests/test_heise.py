"""Heise PM indicator driver — config, protocol, sim device, comscan."""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from heise import (HeiseConfig, HeiseError, HeiseGauge, HeiseProtocol,
                   PRESSURE_UNITS, unit_code, unit_name)
from heise import comscan
from heise.comscan import PortInfo, probe_port, search


def _wait(pred, timeout=10.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


# ── config / units ───────────────────────────────────────────────────────
def test_unit_code_mapping():
    assert unit_code("psi") == 0
    assert unit_code("kPa") == 7
    assert unit_code("KPA") == 7          # case-insensitive
    assert unit_code(5) == 5
    assert unit_name(9) == "mmHg"
    assert len(PRESSURE_UNITS) == 13      # codes 0..12 per Appendix A
    with pytest.raises(ValueError):
        unit_code("furlongs")
    with pytest.raises(ValueError):
        unit_code(99)


def test_config_round_trip(tmp_path):
    cfg = HeiseConfig(com_port="COM5", baud=4800, poll_s=0.5)
    cfg.left.unit = "kPa"
    cfg.right.role = "temperature"
    cfg.right.unit = "C"
    p = tmp_path / "heise.json"
    cfg.save(p)
    back = HeiseConfig.load(p)
    assert back.com_port == "COM5" and back.baud == 4800
    assert back.left.unit == "kPa"
    assert back.right.role == "temperature" and back.right.unit == "C"


def test_config_unknown_keys_tolerated():
    cfg = HeiseConfig.from_dict(
        {"com_port": "COM7", "bogus": 1,
         "left": {"name": "P", "mystery": 2}})
    assert cfg.com_port == "COM7" and cfg.left.name == "P"


# ── protocol (scripted wire) ─────────────────────────────────────────────
class _Wire:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []
        self.cleared = 0

    def write(self, data):
        self.sent.append(data.rstrip(b"\r").decode("ascii"))
        return len(data)

    def read_until(self, expected=b"\n"):
        return self.chunks.pop(0) if self.chunks else b""

    def reset_input_buffer(self):
        self.cleared += 1

    def close(self):
        pass


def test_read_values_parses_two_floats():
    p = HeiseProtocol(_Wire([b"0.004469,-0.000227\r\n"]))
    assert p.read_values() == pytest.approx([0.004469, -0.000227])


def test_read_values_single_port():
    p = HeiseProtocol(_Wire([b"14.6960\r\n"]))
    assert p.read_values() == pytest.approx([14.696])


def test_split_line_reassembled():
    """pyserial returns partial bytes on timeout — fragments must be
    reassembled, not treated as complete lines."""
    p = HeiseProtocol(_Wire([b"14.69", b"60,-0.0002\r\n"]))
    assert p.read_values() == pytest.approx([14.696, -0.0002])


def test_command_requires_ok_and_resyncs():
    wire = _Wire([b"WHAT\r\n"])
    p = HeiseProtocol(wire)
    with pytest.raises(HeiseError, match="unexpected reply"):
        p.command("EUNIT 1, 1")
    assert wire.cleared >= 1              # line drained for next user


def test_indicator_error_raises():
    p = HeiseProtocol(_Wire([b"Err01\r\n"]))
    with pytest.raises(HeiseError, match="Err01"):
        p.query("BOGUS")


def test_timeout_raises_and_resyncs():
    wire = _Wire([])
    p = HeiseProtocol(wire)
    with pytest.raises(HeiseError, match="no response"):
        p.query("?")
    assert wire.cleared >= 1


def test_eunit_wire_format():
    wire = _Wire([b"OK\r\n"])
    HeiseProtocol(wire).set_units(7, 0)
    assert wire.sent == ["EUNIT 7, 0"]    # Appendix A spacing


# ── sim device ───────────────────────────────────────────────────────────
@pytest.fixture
def dev():
    d = HeiseGauge(HeiseConfig(force_sim=True, poll_s=0.05))
    yield d
    d.disconnect()


def test_connect_and_poll(dev):
    dev.connect()
    assert dev.connected and dev.sim_mode
    assert dev.channel_names() == ["Temperature", "Pressure"]
    assert _wait(lambda: dev.frame_count() >= 5)
    latest = dev.latest()
    assert 14.0 < latest["Pressure"] < 15.5          # ~ambient psi
    assert 70.0 < latest["Temperature"] < 75.0       # ~room °F


def test_blocks_delivered_when_running(dev):
    dev.connect()
    got = []
    dev.on_block = got.append
    dev.start()
    assert _wait(lambda: len(got) >= 3)
    dev.stop()
    n = len(got)
    time.sleep(0.2)
    assert len(got) <= n + 1              # stop gates the callback
    block = got[0]
    assert set(block) == {"t", "Pressure", "Temperature"}


def test_set_pressure_unit_live(dev):
    dev.connect()
    assert _wait(lambda: dev.frame_count() >= 2)
    before = dev.latest()["Pressure"]     # psi, ~14.7
    name = dev.set_pressure_unit("kPa")
    assert name == "kPa"
    assert dev.config.right.unit == "kPa"
    assert dev.get_unit_codes()[1] == 7
    n0 = dev.frame_count()
    assert _wait(lambda: dev.frame_count() > n0 + 2)
    after = dev.latest()["Pressure"]
    assert after == pytest.approx(before * 6.89476, rel=0.01)
    # temperature port untouched by the pressure-unit change
    assert 70.0 < dev.latest()["Temperature"] < 75.0


def test_zero_and_read_now(dev):
    dev.connect()
    vals = dev.read_now()
    assert set(vals) == {"Pressure", "Temperature"}
    dev.zero("left")
    time.sleep(0.15)
    assert abs(dev.read_now()["Pressure"]) < 0.5     # re-zeroed near 0


def test_battery_and_helpers(dev):
    dev.connect()
    assert dev.battery() == pytest.approx(6.71)
    dev.set_damping(2)
    dev.set_tare(False, False)


def test_start_requires_connect(dev):
    with pytest.raises(RuntimeError):
        dev.start()


def test_single_port_config():
    """Disabling one driver-side port must not shift the other port's
    value (the indicator still transmits both — map by position)."""
    cfg = HeiseConfig(force_sim=True, poll_s=0.05)
    cfg.left.role = "off"                  # temperature off
    d = HeiseGauge(cfg)
    try:
        d.connect()
        assert d.channel_names() == ["Pressure"]
        assert _wait(lambda: d.frame_count() >= 2)
        latest = d.latest()
        assert "Pressure" in latest
        assert 14.0 < latest["Pressure"] < 15.5   # NOT the ~72F value
    finally:
        d.disconnect()


def test_connect_with_rtd_port_survives_eunit(dev):
    """Live 2026-07-23: the RTD (left) port rejects pressure unit
    codes — 'EUNIT 0, 0' answered Err02 and killed the connect. Units
    must be read-modify-write and non-fatal."""
    dev.connect()                       # raised HeiseError before fix
    assert dev.connected
    codes = dev.get_unit_codes()
    assert codes[0] == 15               # RTD code untouched
    assert codes[1] == 0                # configured psi applied


def test_wrong_pressure_role_on_rtd_port_falls_back():
    """Misconfigured session (pressure role on the RTD port): connect
    must still succeed, with the unit label reflecting the instrument."""
    cfg = HeiseConfig(force_sim=True, poll_s=0.05)
    cfg.left.role = "pressure"          # wrong — the port is an RTD
    cfg.left.unit = "kPa"
    d = HeiseGauge(cfg)
    msgs = []
    d.on_status = msgs.append
    try:
        d.connect()
        assert d.connected
        assert any("Could not set the pressure unit" in m
                   for m in msgs)
        assert cfg.left.unit == "code15"    # instrument's actual code
    finally:
        d.disconnect()


class _SlowSilentWire:
    """Device gone quiet: every read blocks the full timeout and
    returns nothing (worst case for disconnect-while-reading)."""

    def __init__(self, read_s=0.4):
        self.read_s = read_s
        self.closed = False
        self.close_calls = 0

    def write(self, data):
        return len(data)

    def read_until(self, expected=b"\r"):
        time.sleep(self.read_s)
        return b""

    def reset_input_buffer(self):
        pass

    def close(self):
        self.close_calls += 1
        self.closed = True


def test_disconnect_closes_port_even_when_device_silent(monkeypatch):
    """Live 2026-07-23: second connect got 'Access is denied' — the
    port handle survived a disconnect that raced a blocked read. The
    orderly shutdown must exit the poll thread AND close the handle
    quickly."""
    import heise.device as heise_device
    from heise.protocol import HeiseProtocol

    good = _SlowSilentWire()
    # respond once for the connect probe, then go silent
    responses = [b"?\r", b"\r", b"73.6,11.4\r", b"?\r", b"\r",
                 b"15,0\r"]

    def read_until(expected=b"\r"):
        if responses:
            return responses.pop(0)
        time.sleep(0.4)
        return b""

    good.read_until = read_until
    monkeypatch.setattr(
        HeiseProtocol, "open",
        classmethod(lambda cls, *a, **k: HeiseProtocol(good)))
    cfg = HeiseConfig(com_port="COM3", poll_s=0.05,
                      apply_units_on_connect=False)
    dev = HeiseGauge(cfg)
    dev.connect()
    assert dev.connected
    time.sleep(0.3)                     # poll thread now blocked reading
    t0 = time.perf_counter()
    dev.disconnect()
    took = time.perf_counter() - t0
    assert good.closed, "port handle was NOT closed on disconnect"
    assert took < 5.0, f"disconnect took {took:.1f}s"
    assert not dev.connected


def test_open_retries_on_access_denied():
    """A just-released Windows USB-serial port briefly answers
    'Access is denied' — open must retry before giving up."""
    from heise.protocol import HeiseProtocol
    attempts = []

    def factory():
        attempts.append(1)
        if len(attempts) < 3:
            raise OSError("could not open port 'COM3': "
                          "PermissionError(13, 'Access is denied.')")
        return _SlowSilentWire(read_s=0.0)

    p = HeiseProtocol.open("COM3", _serial_factory=factory)
    assert p.is_open and len(attempts) == 3


def test_open_gives_helpful_error_when_port_truly_held():
    from heise.protocol import HeiseProtocol

    def factory():
        raise OSError("could not open port 'COM3': "
                      "PermissionError(13, 'Access is denied.')")

    with pytest.raises(HeiseError, match="held by another program"):
        HeiseProtocol.open("COM3", _serial_factory=factory)


def test_closing_flag_aborts_reads_fast():
    from heise.protocol import HeiseProtocol
    wire = _SlowSilentWire(read_s=0.2)
    p = HeiseProtocol(wire)
    p.closing.set()
    t0 = time.perf_counter()
    with pytest.raises(HeiseError, match="closing"):
        p._read_line()
    assert time.perf_counter() - t0 < 0.1


def test_reconnect_cycle_sim():
    """connect → disconnect → connect on the same gauge object."""
    d = HeiseGauge(HeiseConfig(force_sim=True, poll_s=0.05))
    try:
        for _ in range(2):
            d.connect()
            assert d.connected
            assert _wait(lambda: d.frame_count() >= 2)
            d.disconnect()
            assert not d.connected
    finally:
        d.disconnect()


def test_bench_wire_format_echo_and_cr():
    """Exact live COM4 traffic (2026-07-23): command echoed back, bare
    CR separator, CR-only EOM — '?\\r' '\\r' '73.614870,11.430730\\r'."""
    p = HeiseProtocol(_Wire([b"?\r", b"\r", b"73.614870,11.430730\r"]))
    assert p.read_values() == pytest.approx([73.614870, 11.430730])


def test_bench_wire_glued_by_timeout():
    """The same traffic arriving as ONE timeout-glued chunk must also
    parse (this exact blob raised 'unparseable measurement' live)."""
    p = HeiseProtocol(_Wire([b"?\r", b"\r73.614870,11.430730\r"]))
    assert p.read_values() == pytest.approx([73.614870, 11.430730])


# ── comscan ──────────────────────────────────────────────────────────────
class _HeiseResponder:
    def __init__(self):
        self.lines = [b"0.004469,-0.000227\r\n"]

    def reset_input_buffer(self):
        pass

    def write(self, data):
        return len(data)

    def read_until(self, expected=b"\n"):
        return self.lines.pop(0) if self.lines else b""

    def close(self):
        pass


class _StingResponder(_HeiseResponder):
    """A sting chain echoes the command — must NOT classify as Heise."""

    def __init__(self):
        self.lines = [b"?\r", b"Err\r"]


class _Dead(_HeiseResponder):
    def __init__(self):
        self.lines = []


def test_probe_identifies_heise():
    r = probe_port(PortInfo("COM5", "Prolific USB-to-Serial"),
                   _serial_factory=lambda *a: _HeiseResponder())
    assert r.opened and r.is_heise and r.baud == 9600
    assert "HEISE INDICATOR FOUND" in r.summary


def test_probe_rejects_sting_and_dead():
    r = probe_port(PortInfo("COM9"),
                   _serial_factory=lambda *a: _StingResponder())
    assert r.opened and not r.is_heise
    r2 = probe_port(PortInfo("COM3"),
                    _serial_factory=lambda *a: _Dead())
    assert r2.opened and not r2.is_heise and "silent" in r2.summary


def test_probe_unopenable():
    def boom(*_a):
        raise OSError("Access is denied")
    r = probe_port(PortInfo("COM1"), _serial_factory=boom)
    assert not r.opened and "cannot open" in r.summary


def test_search_finds_indicator(monkeypatch):
    ports = [PortInfo("COM9", "Prolific (sting)"),
             PortInfo("COM5", "Prolific (heise)")]
    monkeypatch.setattr(comscan, "list_com_ports", lambda: ports)

    def factory(device, *a):
        return (_HeiseResponder() if device == "COM5"
                else _StingResponder())

    hits = [r for r in search(_serial_factory=factory) if r.is_heise]
    assert [r.port.device for r in hits] == ["COM5"]
