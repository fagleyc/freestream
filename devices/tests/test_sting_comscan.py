"""COM search utility + echo-tolerant init (the live COM9 failure)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lswt_sting.device as sting_device
from lswt_sting import comscan
from lswt_sting.comscan import PortInfo, probe_port, search
from lswt_sting.config import StingConfig
from lswt_sting.device import StingDrive
from lswt_sting.protocol import StingProtocol


# ── fakes ────────────────────────────────────────────────────────────────
class _StingResponderSerial:
    """A port with a live sting chain: echoes 1R then answers *R."""

    def __init__(self):
        self.lines = [b"1R\r", b"*R\r"]

    def reset_input_buffer(self):
        pass

    def write(self, data):
        return len(data)

    def read_until(self, expected=b"\r"):
        return self.lines.pop(0) if self.lines else b""

    def close(self):
        pass


class _DeadSerial(_StingResponderSerial):
    def __init__(self):
        self.lines = []


class _EchoDropWire:
    """Reproduces the live COM9 trace: ``1R`` answers (echo + ``*R``),
    ``SSI1`` echoes, then the interface switches ``SSA0``/``Z`` produce
    NO echo — while ``R`` status and ``PR`` keep working."""

    def __init__(self):
        self.buf = bytearray()
        self.sent = []

    def write(self, data):
        line = data.rstrip(b"\r").decode("ascii")
        self.sent.append(line)
        cmd = line[1:] if line[:1] in "12" else line
        if cmd == "R":
            self.buf += (line + "\r*R\r").encode("ascii")
        elif cmd.startswith("SSA") or cmd == "Z":
            pass                        # echo never arrives (field bug)
        elif cmd == "PR":
            self.buf += (line + "\r+0\r").encode("ascii")
        else:                           # SSI1, LD3, A/AD/V, FSD1, D, G…
            self.buf += (line + "\r").encode("ascii")
        return len(data)

    def read_until(self, expected=b"\r"):
        i = self.buf.find(b"\r")
        if i < 0:
            return b""
        out = bytes(self.buf[:i + 1])
        del self.buf[:i + 1]
        return out

    def reset_input_buffer(self):
        self.buf.clear()

    def close(self):
        pass


# ── comscan ──────────────────────────────────────────────────────────────
def test_probe_identifies_sting():
    r = probe_port(PortInfo("COM9", "Prolific USB-to-Serial"),
                   _serial_factory=lambda *a: _StingResponderSerial())
    assert r.opened and r.is_sting
    assert "1R" in r.response and "*R" in r.response
    assert "STING CHAIN FOUND" in r.summary


def test_probe_silent_port_is_not_sting():
    r = probe_port(PortInfo("COM3"),
                   _serial_factory=lambda *a: _DeadSerial())
    assert r.opened and not r.is_sting and r.response == ""
    assert "silent" in r.summary


def test_probe_unopenable_port_reports_error():
    def boom(*_a):
        raise OSError("Access is denied")
    r = probe_port(PortInfo("COM1"), _serial_factory=boom)
    assert not r.opened and "denied" in r.error
    assert "cannot open" in r.summary


def test_search_finds_the_right_port(monkeypatch):
    ports = [PortInfo("COM3", "Motherboard"),
             PortInfo("COM9", "Prolific USB-to-Serial")]
    monkeypatch.setattr(comscan, "list_com_ports", lambda: ports)

    def factory(device, *a):
        return (_StingResponderSerial() if device == "COM9"
                else _DeadSerial())

    results = search(_serial_factory=factory)
    hits = [r for r in results if r.is_sting]
    assert [r.port.device for r in hits] == ["COM9"]


# ── echo-tolerant init (the reported failure) ────────────────────────────
def test_connect_survives_echo_silent_setup_commands(monkeypatch):
    """Field trace 2026-07-22: SSA0 echo timeout after SSI1 on COM9.
    Interface-setup commands are now sent blind, so connect must
    succeed against a wire that never echoes SSA/Z."""
    wire = _EchoDropWire()
    monkeypatch.setattr(
        StingProtocol, "open",
        classmethod(lambda cls, *a, **k: StingProtocol(wire)))
    cfg = StingConfig(force_sim=False, poll_ms=50, init_reset=False,
                      com_port="COM9", park_on_disconnect=False,
                      restore_position=False,
                      state_path=str(Path(__file__).parent
                                     / "_state_scratch.json"))
    dev = StingDrive(cfg)
    try:
        dev.connect()                   # raised StingError before the fix
        assert dev.connected
        # same bytes still on the wire, legacy order preserved
        assert "SSI1" in wire.sent
        assert "1SSA0" in wire.sent and "2SSA0" in wire.sent
        assert wire.sent.index("SSI1") < wire.sent.index("1SSA0")
        assert "FSD1" in wire.sent
    finally:
        dev.disconnect()


def test_direction_matches_legacy_stall_scenario():
    """Field stall 2026-07-22: Alpha zeroed at +30°, commanded to +25°.
    Legacy convention (Core.exe IL): counts = -(angle - zero) x K, so
    the correct wire command is D+13705 — the inverted +K mapping sent
    D-13705, which the hardware runs toward +35° into the hard stop."""
    from lswt_sting.config import StingAxisConfig
    ax = StingAxisConfig(zero_offset_deg=30.0, zeroed=True)
    delta = ax.angle_to_counts(25.0) - 0        # counter is 0 after PZ
    assert delta == pytest.approx(5 * 2741.0525, abs=1)
    assert delta > 0, "a -5 deg move must be POSITIVE steps on the wire"
    # and the readback maps the counter to angle consistently
    assert ax.counts_to_angle(delta) == pytest.approx(25.0, abs=1e-3)


def test_direction_round_trip_both_axes():
    from lswt_sting.config import _alpha, _beta
    for ax in (_alpha(), _beta()):
        ax.zero_offset_deg = 2.0
        for angle in (-10.0, 0.0, 12.5):
            c = ax.angle_to_counts(angle)
            assert ax.counts_to_angle(c) == pytest.approx(angle,
                                                          abs=1e-2)
    # field-verified signs (2026-07-22): alpha +angle = -counts,
    # beta +angle = +counts
    a, b = _alpha(), _beta()
    assert a.direction == -1 and a.angle_to_counts(5.0) < 0
    assert b.direction == +1 and b.angle_to_counts(5.0) > 0


def test_move_by_uses_legacy_direction(monkeypatch):
    """Relative jog must also follow counts = -delta x K (legacy's own
    Beta jog branch omitted the negation — a dormant bug contradicted
    by its readback; we keep both axes self-consistent)."""
    import lswt_sting.device as sting_device
    from lswt_sting.config import StingConfig

    class _Recorder(sting_device.SimSerial):
        def __init__(self, config=None):
            super().__init__(config)
            self.sent = []

        def write(self, data):
            self.sent.append(data.rstrip(b"\r\n").decode("ascii"))
            return super().write(data)

    created = []

    def factory(c):
        s = _Recorder(c)
        created.append(s)
        return s

    monkeypatch.setattr(sting_device, "SimSerial", factory)
    cfg = StingConfig(force_sim=True, poll_ms=50, init_reset=False,
                      park_on_disconnect=False, restore_position=False,
                      state_path=str(Path(__file__).parent
                                     / "_state_scratch.json"))
    dev = sting_device.StingDrive(cfg)
    try:
        dev.connect()
        n0 = len(created[0].sent)
        dev.move_by("alpha", 1.0)               # +1° jog
        tail = created[0].sent[n0:]
        assert "1D-2741" in tail, tail
        dev.stop_all()
    finally:
        dev.disconnect()


class _ScriptedWire:
    """Wire whose read_until returns a scripted sequence of chunks
    (pyserial returns PARTIAL bytes on timeout — emulate that)."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.cleared = 0

    def write(self, data):
        return len(data)

    def read_until(self, expected=b"\r"):
        return self.chunks.pop(0) if self.chunks else b""

    def reset_input_buffer(self):
        self.cleared += 1

    def close(self):
        pass


def test_read_line_reassembles_split_response():
    """Field 2026-07-22 11:03: position responses arrived split across
    the read timeout ('*+0' … '000000000') and the fragments were taken
    for whole lines, desyncing every later transaction."""
    from lswt_sting.protocol import StingProtocol
    p = StingProtocol(_ScriptedWire(
        [b"1PR\r", b"*+00", b"00013705\r"]))
    assert p.position("1") == 13705


def test_incomplete_line_raises_not_desyncs():
    from lswt_sting.protocol import StingProtocol, StingError
    p = StingProtocol(_ScriptedWire([b"*+00", b"", b""]))
    with pytest.raises(StingError, match="incomplete"):
        p._read_line()


def test_stale_line_absorbed_before_echo():
    """Field crash: '2PZ' issued right after a glitched poll read the
    poll's late '2PR' echo. One stale line must be absorbed."""
    from lswt_sting.protocol import StingProtocol
    p = StingProtocol(_ScriptedWire([b"2PR\r", b"2PZ\r"]))
    p.command("2", "PZ")                       # raised before the fix


def test_failed_transaction_resyncs_line():
    """After a genuine failure the buffer is drained (post-settle) so
    the NEXT transaction starts clean instead of inheriting leftovers."""
    from lswt_sting.protocol import StingProtocol, StingError
    wire = _ScriptedWire([b"000000000\r", b"*+0000000\r",   # junk x2
                          b"1R\r", b"*R\r"])               # then clean
    p = StingProtocol(wire)
    with pytest.raises(StingError, match="unexpected echo"):
        p.command("1", "R")
    assert wire.cleared >= 1                   # resync drained the port
    assert p.status("1") == "*R"               # next transaction clean


def test_read_line_tolerates_crlf_leftover():
    """Drives terminate CR(+LF); a leading LF from the previous line
    must not corrupt echo validation (seen live: '\\n1P')."""
    from lswt_sting.protocol import StingProtocol

    class _Wire:
        def __init__(self):
            self.lines = [b"\n1PR\r"]

        def write(self, data):
            return len(data)

        def read_until(self, expected=b"\r"):
            return self.lines.pop(0) if self.lines else b""

        def reset_input_buffer(self):
            pass

        def close(self):
            pass

    p = StingProtocol(_Wire())
    p.command("1", "PR")                        # must not raise


def test_connect_still_fails_cleanly_when_dead(monkeypatch):
    """A truly silent chain must still produce the helpful probe error,
    not hang."""
    class _AllDead(_EchoDropWire):
        def write(self, data):
            self.sent.append(data.rstrip(b"\r").decode("ascii"))
            return len(data)            # never answers anything

    wire = _AllDead()
    monkeypatch.setattr(
        StingProtocol, "open",
        classmethod(lambda cls, *a, **k: StingProtocol(wire)))
    cfg = StingConfig(force_sim=False, poll_ms=50, init_reset=False,
                      park_on_disconnect=False, restore_position=False,
                      state_path=str(Path(__file__).parent
                                     / "_state_scratch.json"))
    dev = StingDrive(cfg)
    with pytest.raises(Exception, match="not responding|power"):
        dev.connect()
    assert not dev.connected
