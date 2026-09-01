"""South LSWT traverse driver — protocol, referencing, limits, GUI.

The rig: three IDC SmartStep23 SmartDrives on one RS-232C daisy chain
(unit 1 = Z, 2 = Y, 3 = X). Everything here runs against the byte-level
``SimChain`` emulator, which echoes every written byte before its
``*``-prefixed reply exactly like the real chain (echo is mandatory for
daisy chaining), so the transport's echo handling is genuinely
exercised, not mocked away.

The design under test is the deliberately simple referencing story:
no homing routine — jog to the reference spot, Set home (``SP``), and
host-side soft limits gate everything after.
"""

import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lswt_traverse import (AXES, LswtTraverseDrive,        # noqa: E402
                           SimChain, TraverseConfig)
from lswt_traverse import protocol as P                    # noqa: E402


def _cfg(**kw) -> TraverseConfig:
    kw.setdefault("force_sim", True)
    kw.setdefault("poll_s", 0.03)
    return TraverseConfig(**kw)


def _drive(**kw) -> LswtTraverseDrive:
    d = LswtTraverseDrive(_cfg(**kw))
    d.connect()
    return d


# ── protocol layer ──────────────────────────────────────────────────────
def test_command_formatting():
    assert P.command(2, "PA", "1") == b"2PA1\r"
    assert P.command(None, "S") == b"S\r"
    assert P.command(3, "DA", "-1.25") == b"3DA-1.25\r"


def test_extract_response_discards_the_chain_echo():
    """Every read starts with our own echoed bytes; the parser must
    find the * response behind them."""
    assert P.extract_response(b"2PA1\r*+1.500\r") == "+1.500"
    assert P.extract_response(b"2PA1\r*+1.5") is None      # incomplete
    assert P.extract_response(b"2GO\r") is None            # echo only


def test_real_formatting_keeps_four_decimals_max():
    assert P.format_real(1.0) == "1"
    assert P.format_real(-0.5) == "-0.5"
    assert P.format_real(0.12345) == "0.1235"              # manual: 4 dp


def test_unit_addresses_match_the_rig_labels():
    """Photo stepper_drivers.jpg: drive 1 = Z, 2 = Y, 3 = X."""
    assert (P.UNIT_Z, P.UNIT_Y, P.UNIT_X) == (1, 2, 3)
    cfg = TraverseConfig()
    assert cfg.z.unit == 1 and cfg.y.unit == 2 and cfg.x.unit == 3


# ── emulator wire behaviour ─────────────────────────────────────────────
def test_sim_chain_echoes_before_responding():
    chain = SimChain()
    chain.write(b"1PA1\r")
    raw = chain.read(64)
    assert raw.startswith(b"1PA1\r"), raw                  # echo first
    assert b"*" in raw


def test_sim_chain_broadcast_stop_reaches_every_drive():
    chain = SimChain()
    chain.write(b"S\r")
    stopped = {u for (u, m, _a) in chain.log if m == "S"}
    assert stopped == {1, 2, 3}


# ── connect / probe ─────────────────────────────────────────────────────
def test_connect_probes_all_three_drives():
    d = _drive()
    try:
        assert d.connected and d.sim_mode
        probed = {u for (u, m, _a) in d._serial.log if m == "MN"}
        assert probed == {1, 2, 3}
    finally:
        d.disconnect()


def test_disconnect_sends_a_broadcast_stop():
    d = _drive()
    ser = d._serial
    d.disconnect()
    assert (1, "S", "") in ser.log and (3, "S", "") in ser.log
    assert not d.connected


# ── the referencing story ───────────────────────────────────────────────
def test_absolute_moves_are_locked_until_home_is_set():
    d = _drive()
    try:
        with pytest.raises(ValueError, match="not referenced"):
            d.move_to(x=1.0)
        d.set_home("X")
        d.move_to(x=0.4)                                   # now allowed
        assert d.wait_settled(15)
        assert d.state()["X"]["position"] == pytest.approx(0.4, abs=0.02)
    finally:
        d.disconnect()


def test_set_home_sends_sp_and_zeroes_the_axis():
    d = _drive()
    try:
        d.jog("Y", positive=True)
        time.sleep(0.3)
        d.stop_axis("Y")
        time.sleep(0.1)
        assert d.state()["Y"]["position"] > 0.05           # actually moved
        d.set_home("Y")
        assert (2, "SP", "0") in d._serial.log
        time.sleep(0.1)
        assert abs(d.state()["Y"]["position"]) < 0.01      # reads zero now
        assert d.is_referenced("Y")
    finally:
        d.disconnect()


def test_set_home_refused_while_moving():
    d = _drive()
    try:
        d.jog("Z", positive=True)
        with pytest.raises(RuntimeError, match="stop the axis"):
            d.set_home("Z")
        d.stop_axis("Z")
    finally:
        d.disconnect()


def test_home_datum_offsets_the_origin():
    """A nonzero datum makes the reference spot a KNOWN offset, not 0."""
    cfg = _cfg()
    cfg.x.home_datum_in = -10.0
    cfg.x.min_in, cfg.x.max_in = -10.0, 10.0
    d = LswtTraverseDrive(cfg)
    d.connect()
    try:
        d.set_home("X")
        assert (3, "SP", "-10") in d._serial.log
        time.sleep(0.1)
        assert d.state()["X"]["position"] == pytest.approx(-10.0, abs=0.01)
        d.move_to(x=-9.5)                    # inside limits from datum
        assert d.wait_settled(15)
    finally:
        d.disconnect()


# ── the software limitations ────────────────────────────────────────────
def test_targets_outside_soft_limits_are_refused():
    d = _drive()
    try:
        d.set_home("X")
        with pytest.raises(ValueError, match="outside soft limits"):
            d.move_to(x=d.config.x.max_in + 1.0)
        with pytest.raises(ValueError, match="outside soft limits"):
            d.move_to(x=d.config.x.min_in - 1.0)
    finally:
        d.disconnect()


def test_compound_move_validates_before_any_wire_command():
    """A compound move with one bad axis must start NOTHING."""
    d = _drive()
    try:
        d.set_home("X")
        d.set_home("Y")
        before = len(d._serial.log)
        with pytest.raises(ValueError):
            d.move_to(x=0.5, y=999.0)
        started = [e for e in d._serial.log[before:] if e[1] == "GO"]
        assert not started, started
    finally:
        d.disconnect()


def test_jog_allowed_unreferenced_but_fenced_after_homing():
    """Jogs are how you REACH the reference spot, so they never need a
    home — but once referenced, the monitor stops a jog that crosses a
    soft limit (the drive knows nothing about our limits)."""
    cfg = _cfg()
    cfg.z.min_in, cfg.z.max_in = -0.05, 0.05     # tiny fence
    cfg.z.jog_velocity_ips = 1.0
    d = LswtTraverseDrive(cfg)
    d.connect()
    try:
        d.jog("Z", positive=True)                # unreferenced: fine
        time.sleep(0.15)
        d.stop_axis("Z")
        # set_home refuses while the stage still reads moving — wait
        # for the monitor to confirm the decel-stop finished
        assert d.wait_settled(5)
        d.set_home("Z")                          # 0 at current spot
        d.jog("Z", positive=True)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            st = d.state()["Z"]
            if st["state"] == "soft limit":
                break
            time.sleep(0.03)
        st = d.state()["Z"]
        assert st["state"] == "soft limit", st
        assert st["position"] < 0.6              # stopped near the fence
        # and a further jog INTO the limit is refused outright
        with pytest.raises(ValueError, match="soft limit"):
            d.jog("Z", positive=True)
        d.jog("Z", positive=False)               # away is always fine
        d.stop_axis("Z")
    finally:
        d.disconnect()


# ── motion mechanics ────────────────────────────────────────────────────
def test_move_sends_the_full_profile_then_go():
    d = _drive()
    try:
        d.set_home("X")
        before = len(d._serial.log)
        d.move_to(x=0.3)
        sent = [m for (u, m, _a) in d._serial.log[before:] if u == 3]
        for expected in ("CB", "AC", "DE", "VE", "DA", "GO"):
            assert expected in sent, (expected, sent)
        assert sent.index("DA") < sent.index("GO")
        assert d.wait_settled(15)
    finally:
        d.disconnect()


def test_axes_move_concurrently():
    d = _drive()
    try:
        d.set_home_all()
        t0 = time.monotonic()
        d.move_to(x=0.3, y=-0.3, z=0.3)          # 0.3 s each at 1 in/s
        assert d.wait_settled(15)
        elapsed = time.monotonic() - t0
        s = d.state()
        assert s["X"]["position"] == pytest.approx(0.3, abs=0.02)
        assert s["Y"]["position"] == pytest.approx(-0.3, abs=0.02)
        assert s["Z"]["position"] == pytest.approx(0.3, abs=0.02)
        assert elapsed < 5.0, "axes appear to have moved sequentially"
    finally:
        d.disconnect()


def test_new_move_supersedes_the_one_in_flight():
    d = _drive()
    try:
        d.set_home("X")
        d.move_to(x=5.0)                          # long move
        time.sleep(0.2)
        d.move_to(x=0.1)                          # retarget mid-flight
        assert d.wait_settled(20)
        assert d.state()["X"]["position"] == pytest.approx(0.1, abs=0.02)
    finally:
        d.disconnect()


def test_stop_all_is_a_broadcast_s():
    d = _drive()
    try:
        d.set_home_all()
        d.move_to(x=5.0, y=5.0)
        time.sleep(0.15)
        before = len(d._serial.log)
        d.stop_all()
        stops = {u for (u, m, _a) in d._serial.log[before:] if m == "S"}
        assert stops == {1, 2, 3}                 # broadcast hit them all
        time.sleep(0.15)
        s = d.state()
        assert not s["X"]["moving"] and not s["Y"]["moving"]
        assert 0 < s["X"]["position"] < 5.0       # genuinely interrupted
    finally:
        d.disconnect()


def test_ring_buffer_streams_positions():
    d = _drive()
    try:
        d.set_home("X")
        d.move_to(x=0.5)
        assert d.wait_settled(15)
        time.sleep(0.1)
        data = d.ring.tail(1000)
        assert data["t"].size >= 5
        assert data["X"][-1] == pytest.approx(0.5, abs=0.02)
        assert data["X"].max() <= 0.55            # never overshot the plot
    finally:
        d.disconnect()


# ── config round trip ───────────────────────────────────────────────────
def test_config_round_trips_through_json(tmp_path):
    cfg = _cfg(port="COM7")
    cfg.x.min_in, cfg.x.max_in = -3.5, 4.5
    cfg.y.velocity_ips = 2.0
    path = tmp_path / "defaults.json"
    cfg.save(path)
    loaded = TraverseConfig.load(path)
    assert loaded.port == "COM7"
    assert loaded.x.min_in == -3.5 and loaded.x.max_in == 4.5
    assert loaded.y.velocity_ips == 2.0
    assert loaded.z.unit == 1                     # axis identity intact


def test_load_defaults_survives_garbage(tmp_path, monkeypatch):
    bad = tmp_path / "defaults.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setenv("LSWT_TRAVERSE_DEFAULTS", str(bad))
    cfg = TraverseConfig.load_defaults()
    assert cfg.port                               # factory fallback, no raise


# ── GUI (offscreen) ─────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([sys.argv[0]])


def test_gui_full_cycle(app):
    from lswt_traverse.app.main_window import TraverseMainWindow

    win = TraverseMainWindow(_cfg())
    try:
        win.sim.setChecked(True)
        win._handle_connect()
        assert win.device.connected
        assert win.lamp.text() == "SIMULATION"

        # move locked until home; jog free
        card = win.cards["X"]
        win._refresh_ui()
        assert not card.move_btn.isEnabled()
        win._jog_start("X", True)
        time.sleep(0.2)
        win._jog_stop("X")
        time.sleep(0.1)
        win._set_home("X")
        win._refresh_ui()
        assert card.ref_lbl.text() == "HOME SET"
        assert card.move_btn.isEnabled()

        win._move("X", 0.2)
        assert win.device.wait_settled(15)
        win._refresh_ui()
        assert card.big_lbl.text() == "+0.200"

        # limit edit reaches the live config; nonsense is rejected
        win._limits_edited("X", -2.0, 2.0)
        assert (win.config.x.min_in, win.config.x.max_in) == (-2.0, 2.0)
        win._limits_edited("X", 5.0, -5.0)
        assert (win.config.x.min_in, win.config.x.max_in) == (-2.0, 2.0)

        win._estop()
        win._handle_disconnect()
        assert not win.device.connected
    finally:
        win.device.disconnect()
        win.deleteLater()
    app.processEvents()


def test_gui_defaults_round_trip(app, tmp_path, monkeypatch):
    from lswt_traverse.app.main_window import TraverseMainWindow

    monkeypatch.setenv("LSWT_TRAVERSE_DEFAULTS",
                       str(tmp_path / "defs.json"))
    win = TraverseMainWindow(_cfg(port="COM9"))
    try:
        win._limits_edited("Y", -1.5, 1.5)
        win.save_defaults()
        loaded = TraverseConfig.load_defaults()
        assert loaded.port == "COM9"
        assert (loaded.y.min_in, loaded.y.max_in) == (-1.5, 1.5)
    finally:
        win.device.disconnect()
        win.deleteLater()
    app.processEvents()
