"""SSWT traverse driver tests — direction senses, stall, moves, wrap,
limit switches.

Runs on the plant simulator; no hardware required. The sim's plant
truth follows each axis's ``fwd_increases_counts`` (live-verified), so
these tests exercise the exact direction mapping deployed on the rig.
The PLC speed is FIXED (~2000 steps/s, no host control); tests bump the
emulator's per-instance ``sim_rate`` for fast convergence.
"""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traverse_swt.config import AxisConfig, TraverseConfig
from traverse_swt.device import TraverseDrive

FAST_SIM_RATE = 25_000.0     # counts/s (rig-realistic fixed rate: 2000)


def _sim_config(**kw) -> TraverseConfig:
    return TraverseConfig(force_sim=True, loop_ms=20, **kw)


def _calibrated(cfg: TraverseConfig) -> TraverseConfig:
    # rig-like signed slopes, re-zeroed at counts 0 = 0"
    for ax, slope in ((cfg.x, 13705.6), (cfg.y, -14841.0),
                      (cfg.z, -14841.0)):   # sim-scale Z slope
        ax.clicks_per_inch = slope
        ax.inch_high = 0.0
        ax.counts_high = 0
        ax.calibrated = True
        ax.min_in, ax.max_in = -6.0, 6.0
    return cfg


def _connect_fast(dev: TraverseDrive, rate: float = FAST_SIM_RATE) -> None:
    """connect() then bump the sim plant to a fast fixed rate (the rig's
    fixed 2000 steps/s would make convergence tests crawl)."""
    dev.connect()
    dev._plc.sim_rate = rate


def _wait(cond, timeout=10.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


# ── protocol facts ───────────────────────────────────────────────────────
def test_control_masks_match_deployed_csharp():
    cfg = TraverseConfig()
    assert (cfg.x.fwd_mask, cfg.x.rev_mask) == (0x0001, 0x0002)
    assert (cfg.y.fwd_mask, cfg.y.rev_mask) == (0x0008, 0x0004)
    assert (cfg.z.fwd_mask, cfg.z.rev_mask) == (0x0020, 0x0010)
    assert (cfg.x.position_addr, cfg.y.position_addr,
            cfg.z.position_addr) == (12298, 12300, 12302)


def test_status_word_limit_bit_map():
    """StatusWord %MW1: bit0 = X/Axial, bit1 = Y/Lateral,
    bit2 = Z/Vertical — the negative-direction switches (live again;
    module lockout unlinked, host-side reaction only)."""
    from traverse_swt.plc import REG_STATUS, STATUS_LIMIT_MASK
    assert REG_STATUS == 12289
    assert STATUS_LIMIT_MASK == {"X": 0x0001, "Y": 0x0002, "Z": 0x0004}


def test_limit_sense_reversed_and_x_disabled():
    """Rig 2026-07-22: limit bits are ACTIVE-LOW (bit clear = switch
    engaged) and X's limit input is disabled entirely."""
    dev = TraverseDrive(_sim_config())
    cfg = dev.config
    # Y bit SET (healthy) → not engaged; CLEAR → engaged
    assert dev._limit_engaged(0x0002, cfg.y) is False
    assert dev._limit_engaged(0x0000, cfg.y) is True
    assert dev._limit_engaged(0x0004, cfg.z) is False
    assert dev._limit_engaged(0x0000, cfg.z) is True
    # X: input disabled — never engaged regardless of the bit
    assert dev._limit_engaged(0x0000, cfg.x) is False
    assert dev._limit_engaged(0x0001, cfg.x) is False
    # escape hatch: original bit-set-means-engaged sense
    cfg.limit_active_low = False
    assert dev._limit_engaged(0x0002, cfg.y) is True
    assert dev._limit_engaged(0x0000, cfg.y) is False
    # homing refuses an axis whose limit input is disabled
    cfg.x.home_enabled = True
    try:
        dev.home_axis("x")
        raise AssertionError("home_axis('x') should refuse")
    except (ValueError, RuntimeError) as exc:
        assert "limit" in str(exc).lower() or "connect" in str(exc).lower()


def test_z_direction_sense_live_verified():
    """Z per the rig 2026-07-22 (settled): position mode with the
    fwd-bit inversion OFF, homing pinned at the BIT level on the
    opposite sense (seek jogs FWD), datum +18\" at the switch, soft
    limits ±25\" for now. X/Y unchanged."""
    cfg = TraverseConfig()
    assert cfg.z.fwd_increases_counts is False
    assert cfg.z.home_jog_fwd is True
    assert cfg.z.home_datum_in == 18.0
    assert (cfg.z.min_in, cfg.z.max_in) == (-25.0, 25.0)
    assert cfg.x.fwd_increases_counts is True
    assert cfg.y.fwd_increases_counts is True
    assert cfg.y.home_jog_fwd is True
    assert cfg.y.home_datum_in == -18.0


def test_counts_inches_roundtrip_negative_slope():
    ax = AxisConfig(inch_high=0.0, counts_high=0,
                    clicks_per_inch=-14841.0, calibrated=True)
    assert ax.inches_to_counts(1.0) == -14841
    assert abs(ax.counts_to_inches(-14841) - 1.0) < 1e-9
    assert abs(ax.counts_to_inches(ax.inches_to_counts(0.37)) - 0.37) < 1e-3


# ── moves with the live direction senses ─────────────────────────────────
def test_move_converges_on_flipped_z():
    """move_to(z=+…) must converge now that Z's plant sense is modeled
    (this exact command wrong-way-tripped on the rig before the fix)."""
    dev = TraverseDrive(_calibrated(_sim_config()))
    try:
        _connect_fast(dev)
        done = []
        dev.on_move_complete = done.append
        dev.move_to(z=1.0)
        assert _wait(lambda: "Z" in done), \
            f"Z move never completed: {dev.state()['Z']}"
        assert abs(dev.state()["Z"]["inches"] - 1.0) < 0.1
    finally:
        dev.disconnect()


def test_wrongway_trip_fires_on_bad_direction_sense():
    cfg = _calibrated(_sim_config())
    msgs = []
    dev = TraverseDrive(cfg)
    dev.on_status = msgs.append
    try:
        _connect_fast(dev)
        # flip the driver's BELIEF after connect — the sim plant keeps
        # the truth it captured at construction, so they now disagree
        # (same mismatch the rig showed live on 2026-07-07)
        cfg.z.fwd_increases_counts = not cfg.z.fwd_increases_counts
        dev.move_to(z=1.0)
        assert _wait(lambda: any("WRONG WAY" in m for m in msgs), 5.0), \
            "wrong-way trip never fired"
        assert not dev.state()["Z"]["moving"]
    finally:
        dev.disconnect()


def test_multi_axis_move_synchronous():
    dev = TraverseDrive(_calibrated(_sim_config()))
    try:
        _connect_fast(dev)
        dev.move_to(x=1.0, y=-1.0)
        time.sleep(0.3)
        st = dev.state()
        assert st["X"]["moving"] and st["Y"]["moving"]
        assert _wait(lambda: not dev.state()["X"]["moving"] and
                     not dev.state()["Y"]["moving"], 15.0)
        assert abs(dev.state()["X"]["inches"] - 1.0) < 0.1
        assert abs(dev.state()["Y"]["inches"] + 1.0) < 0.1
    finally:
        dev.disconnect()


def test_soft_limits_refuse_out_of_range_targets():
    """Soft-limit clamping: an out-of-range move_to target raises before
    any motion starts."""
    cfg = _calibrated(_sim_config())     # limits ±6"
    dev = TraverseDrive(cfg)
    try:
        _connect_fast(dev)
        for kw in ({"x": 7.0}, {"y": -6.5}, {"z": 100.0}):
            with pytest.raises(ValueError, match="outside limits"):
                dev.move_to(**kw)
        assert not any(s["moving"] for s in dev.state().values())
        # uncalibrated axes refuse too
        cfg.x.calibrated = False
        with pytest.raises(ValueError, match="not calibrated"):
            dev.move_to(x=1.0)
    finally:
        dev.disconnect()


# ── stall detection (the "lateral doesn't move" symptom) ─────────────────
def test_stall_warning_when_module_not_stepping():
    cfg = _calibrated(_sim_config())
    msgs = []
    dev = TraverseDrive(cfg)
    dev.on_status = msgs.append
    try:
        _connect_fast(dev)
        dev._plc.stalled_axes.add("Y")     # faulted stepper module
        dev.move_to(y=2.0)
        assert _wait(lambda: any("STALL: Y" in m for m in msgs), 5.0), \
            "stall warning never fired"
        dev.stop_axis("Y")
        # healthy axis must NOT warn
        msgs.clear()
        dev.move_to(x=2.0)
        time.sleep(0.8)
        dev.stop_axis("X")
        assert not any("STALL" in m for m in msgs)
    finally:
        dev.disconnect()


# ── 1,000,000-count rollover (module reconfigured 2026-07) ──────────────
def test_unwrap_shortest_path_across_1m_boundary():
    """The modules roll over cleanly at 1M: unsigned raw 0…999,999.
    Crossing 999999→0 going up is a small POSITIVE delta; 0→999999
    going down is a small NEGATIVE delta."""
    from traverse_swt.device import TraverseDrive, _AxisState
    st = _AxisState(AxisConfig())              # wrap_modulus 1M default
    assert st.cfg.wrap_modulus == 1_000_000
    TraverseDrive._apply_counts(st, 999_800)
    TraverseDrive._apply_counts(st, 999_900)
    assert st.counts == 999_900
    TraverseDrive._apply_counts(st, 50)        # 999900 → 50 is +150
    assert st.counts == 1_000_050              # continuous, past 1M
    TraverseDrive._apply_counts(st, 400)
    assert st.counts == 1_000_400
    # and back down across the same boundary: 50 → 999900 is −150
    st2 = _AxisState(AxisConfig())
    TraverseDrive._apply_counts(st2, 50)
    TraverseDrive._apply_counts(st2, 999_900)
    assert st2.counts == -100                  # 50 − 150, below zero
    TraverseDrive._apply_counts(st2, 999_000)
    assert st2.counts == -1_000
    # many consecutive wraps accumulate an absolute position way past 1M
    st3 = _AxisState(AxisConfig())
    TraverseDrive._apply_counts(st3, 0)
    raw = 0
    for _ in range(3 * 4):                     # 3 full ring revolutions
        raw = (raw + 250_000) % 1_000_000
        TraverseDrive._apply_counts(st3, raw)
    assert st3.counts == 3_000_000
    # wrap_modulus=0 disables unwrapping (raw passthrough)
    st4 = _AxisState(AxisConfig(wrap_modulus=0))
    TraverseDrive._apply_counts(st4, 100)
    TraverseDrive._apply_counts(st4, -16_777_000)
    assert st4.counts == -16_777_000


def test_long_move_converges_across_multiple_wraps():
    """A calibrated move whose ABSOLUTE target is ~2.4M counts must
    converge: the emulator's raw register wraps 0…999,999 repeatedly
    while the driver's unwrapped absolute position drives the move."""
    cfg = _calibrated(_sim_config())
    cfg.x.clicks_per_inch = 2_400_000.0        # 1" = 2.4M counts
    msgs = []
    dev = TraverseDrive(cfg)
    dev.on_status = msgs.append
    try:
        _connect_fast(dev, rate=600_000.0)     # fast fixed sim plant
        sim_x = dev._plc._axes["X"]
        # the huge synthetic slope gives X a big per-axis rate scale —
        # normalize so the EFFECTIVE plant rate stays 600k counts/s
        # (under the 100k counts/tick jump guard at the 20 ms loop)
        dev._plc.sim_rate = 600_000.0 / sim_x.rate_scale
        done = []
        dev.on_move_complete = done.append
        dev.move_to(x=1.0)                     # absolute target 2.4M counts
        assert _wait(lambda: "X" in done, 30.0), \
            f"move across wraps never completed: {dev.state()['X']}"
        st = dev.state()["X"]
        assert abs(st["inches"] - 1.0) <= cfg.x.tolerance_in * 3
        assert st["counts"] > 2_000_000, \
            f"absolute counts did not pass 1M: {st['counts']}"
        # the raw register stayed on the unsigned 1M ring throughout
        assert 0 <= sim_x.raw < 1_000_000
        assert sim_x.raw == int(round(sim_x.counts)) % 1_000_000
        # no phantom counter-jump / wrong-way trips along the way
        assert not any("JUMPED" in m or "WRONG WAY" in m for m in msgs)
    finally:
        dev.disconnect()


def test_counter_jump_guard_holds_on_counter_reset():
    from traverse_swt.device import TraverseDrive, _AxisState
    # unit: implausible delta is held, plausible delta integrates
    st = _AxisState(AxisConfig())
    assert TraverseDrive._apply_counts(st, 1000, max_jump=5000) is False
    assert TraverseDrive._apply_counts(st, 3000, max_jump=5000) is False
    assert st.counts == 3000
    assert TraverseDrive._apply_counts(st, 500_000, max_jump=5000) is True
    assert st.counts == 3000            # held — not phantom motion
    assert st.raw_prev == 500_000       # but re-based
    assert TraverseDrive._apply_counts(st, 500_100, max_jump=5000) is False
    assert st.counts == 3100            # continues from the new base

    # integration: a synthetic counter reset (module power event) is
    # held + re-based with a warning, not integrated as motion
    cfg = _sim_config()
    msgs = []
    dev = TraverseDrive(cfg)
    dev.on_status = msgs.append
    try:
        dev.connect()
        sim_x = dev._plc._axes["X"]
        sim_x.counts = 300_000          # raw jumps 0 → 300k in one tick
        assert _wait(lambda: any("JUMPED" in m for m in msgs), 3.0), \
            "counter jump never reported"
        time.sleep(0.2)
        st = dev.state()["X"]
        assert abs(st["counts"]) < 100, \
            f"counter reset integrated as motion: {st['counts']}"
    finally:
        dev.disconnect()


def test_pymodbus_exceptions_wrapped_not_thread_killing():
    """Regression for the live 2026-07-07 crash: a raw pymodbus
    ModbusIOException escaped PlcError handling and killed the control
    thread. All transport errors must surface as PlcError."""
    from traverse_swt.plc import PlcError, WagoTraversePlc

    class _DyingClient:
        def read_holding_registers(self, *a, **k):
            raise RuntimeError("No response received after 2 retries")

        def write_registers(self, *a, **k):
            raise OSError("connection reset")

        def write_register(self, *a, **k):
            raise OSError("connection reset")

    plc = WagoTraversePlc("10.0.0.1")
    plc._client = _DyingClient()
    with pytest.raises(PlcError):
        plc.read_block()
    with pytest.raises(PlcError):
        plc.write_control(0, force=True)


# ── motion shaping: dwell / stall-abort / oscillation guard ──────────────
def test_direction_reversal_passes_through_stop_dwell():
    cfg = _calibrated(_sim_config(direction_dwell_ms=400))
    dev = TraverseDrive(cfg)
    try:
        _connect_fast(dev)
        dev.move_to(x=5.0)               # long move: axis is running +
        assert _wait(lambda: dev.state()["X"]["counts"] > 2000, 5.0)
        sim_x = dev._plc._axes["X"]
        dev.move_to(x=-5.0)              # instant flip request
        t0 = time.perf_counter()
        # the plant must STOP first (dwell), not slam into reverse
        assert _wait(lambda: sim_x.cmd_rate == 0, 2.0), \
            "no commanded stop on reversal"
        # and stay stopped for most of the dwell window
        time.sleep(0.25)                 # inside the 400 ms dwell
        assert sim_x.cmd_rate == 0, "reverse command applied inside dwell"
        assert _wait(lambda: sim_x.cmd_rate < 0, 3.0), \
            "reverse never applied after dwell"
        elapsed = time.perf_counter() - t0
        assert elapsed >= 0.3, f"dwell too short: {elapsed:.2f}s"
        dev.stop_axis("X")
    finally:
        dev.disconnect()


def test_stall_aborts_command_after_timeout():
    cfg = _calibrated(_sim_config())
    cfg.stall_abort_ticks = 30           # 0.6 s at the 20 ms test loop
    msgs = []
    dev = TraverseDrive(cfg)
    dev.on_status = msgs.append
    try:
        _connect_fast(dev)
        dev._plc.stalled_axes.add("Y")
        dev.move_to(y=2.0)
        assert _wait(lambda: any("ABORTED: Y" in m for m in msgs), 5.0), \
            "stalled move never aborted"
        st = dev.state()["Y"]
        assert not st["moving"], "axis still commanded after stall abort"
    finally:
        dev.disconnect()


def test_module_status_exposed_and_transitions_logged():
    from traverse_swt.emulator import SIM_S1_FAULT, SIM_S1_IDLE
    cfg = _sim_config()
    dev = TraverseDrive(cfg)
    try:
        dev.connect()
        time.sleep(0.1)
        assert dev.state()["X"]["module_status"][0] == SIM_S1_IDLE
        events = []
        dev.on_module_status = events.append
        dev._plc.stalled_axes.add("Z")   # sim reports the fault S1 code
        assert _wait(lambda: dev.state()["Z"]["module_status"][0] ==
                     SIM_S1_FAULT, 3.0), "fault status never surfaced"
        assert any(ax == "Z" and new == SIM_S1_FAULT
                   for (_t, ax, _old, new, _c) in dev.module_events)
        assert events, "on_module_status callback never fired"
    finally:
        dev.disconnect()


def test_move_oscillation_aborts_with_tolerance_hint():
    cfg = _calibrated(_sim_config(direction_dwell_ms=100))
    cfg.x.tolerance_in = 0.00008         # ~1 count: unreachable band
    msgs = []
    dev = TraverseDrive(cfg)
    dev.on_status = msgs.append
    try:
        _connect_fast(dev)
        dev.move_to(x=0.5)
        assert _wait(lambda: any("oscillating" in m for m in msgs), 20.0), \
            "oscillating move never aborted"
        assert not dev.state()["X"]["moving"]
    finally:
        dev.disconnect()


# ── runtime limit-switch reaction (host-side; lockout unlinked) ──────────
def test_runtime_limit_trip_stops_axis_and_flags_fault():
    """An unexpected limit-bit trip during move_to (switch inside the
    believed-legal soft range) must stop the axis within a tick and
    flag a LIMIT fault — the module no longer stops on its own."""
    cfg = _calibrated(_sim_config())
    msgs = []
    dev = TraverseDrive(cfg)
    dev.on_status = msgs.append
    try:
        _connect_fast(dev)
        sim_y = dev._plc._axes["Y"]
        # park the sim's negative-end switch at ≈ −1" — INSIDE the ±6"
        # soft range, so the legal −2" target trips it unexpectedly
        sim_y.neg_limit_counts = 14_841.0 * sim_y.neg_dir
        dev.move_to(y=-2.0)
        assert _wait(lambda: dev.state()["Y"]["fault"] == "LIMIT", 5.0), \
            f"limit trip never flagged: {dev.state()['Y']}"
        st = dev.state()["Y"]
        assert not st["moving"], "axis still moving after the limit trip"
        assert st["limit"] is True
        assert any("LIMIT" in m and "Y" in m for m in msgs)
        # the sim plant was actually stopped (jog dropped)
        assert _wait(lambda: dev._plc._axes["Y"].cmd_rate == 0, 2.0)
        # commanding AWAY from the switch is the recovery path — the
        # move runs and the fault clears once the bit drops
        dev.move_to(y=0.5)
        assert _wait(lambda: dev.state()["Y"]["fault"] is None, 5.0), \
            "LIMIT fault never cleared after backing off the switch"
    finally:
        dev.disconnect()


def test_limit_engage_stops_axis_even_with_mislabeled_direction():
    """Rig regression 2026-07-22: a position move drove INTO the limit
    without stopping because the direction bookkeeping claimed the axis
    was heading AWAY (sign-convention error). The ENGAGE TRANSITION now
    stops any commanded motion unconditionally — bookkeeping can no
    longer defeat the protection."""
    cfg = _calibrated(_sim_config())
    cfg.wrongway_ticks = 100_000       # keep the wrong-way trip out of it
    dev = TraverseDrive(cfg)
    try:
        _connect_fast(dev)
        sim_y = dev._plc._axes["Y"]
        sim_y.neg_limit_counts = 14_841.0 * sim_y.neg_dir   # switch ≈ −1"
        # flip the BELIEF after connect (plant keeps construction truth):
        # the driver now thinks this move heads away from the switch
        # while the plant physically drives into it
        cfg.y.fwd_increases_counts = not cfg.y.fwd_increases_counts
        dev.move_to(y=2.0)
        assert _wait(lambda: dev.state()["Y"]["fault"] == "LIMIT", 5.0), \
            f"engage transition never stopped the axis: {dev.state()['Y']}"
        assert not dev.state()["Y"]["moving"]
        assert _wait(lambda: dev._plc._axes["Y"].cmd_rate == 0, 2.0), \
            "sim plant still commanded after the limit stop"
    finally:
        dev.disconnect()


# ── jog-bit read-modify-write isolation ──────────────────────────────────
def test_jog_bits_isolated_between_axes(monkeypatch):
    """Commanding one axis's jog bits must never disturb another axis's
    bits in the composed ControlWord (recording fake PLC asserts the
    word transitions)."""
    import traverse_swt.device as device_mod
    from traverse_swt.plc import BlockReading

    class RecordingPlc:
        """Static counts; records every ControlWord transition."""

        def __init__(self, config):
            self.config = config
            self.words = []              # every CHANGED word, in order
            self._control = 0
            self._connected = False

        def connect(self):
            self._connected = True

        def close(self):
            self._connected = False

        @property
        def connected(self):
            return self._connected

        def read_block(self):
            return BlockReading(control=self._control, status=0,
                                counts={"X": 0, "Y": 0, "Z": 0},
                                module_status=None)

        def write_control(self, word, force=False):
            if force or word != self._control:
                self.words.append(word)
            self._control = word

        def last_control(self):
            return self._control

    monkeypatch.setattr(device_mod, "SimPlc", RecordingPlc)
    cfg = _calibrated(_sim_config(direction_dwell_ms=40))
    cfg.stall_ticks = 10_000             # counts never move: no stall
    cfg.stall_abort_ticks = 20_000
    dev = TraverseDrive(cfg)
    X_BITS = cfg.x.fwd_mask | cfg.x.rev_mask
    Y_BITS = cfg.y.fwd_mask | cfg.y.rev_mask
    try:
        dev.connect()
        fake = dev._plc
        dev.move_to(x=2.0)               # X jogs + (fwd bit 0x1)
        assert _wait(lambda: fake._control & X_BITS, 3.0)
        x_bits = fake._control & X_BITS
        dev.move_to(y=2.0)               # Y jogs − counts (rev bit 0x4)
        assert _wait(lambda: fake._control & Y_BITS, 3.0)
        y_bits = fake._control & Y_BITS
        assert fake._control & X_BITS == x_bits, \
            "starting Y disturbed X's jog bits"
        n_words = len(fake.words)
        dev.stop_axis("X")               # X drops; Y must be untouched
        assert _wait(lambda: fake._control & X_BITS == 0, 3.0)
        assert fake._control & Y_BITS == y_bits, \
            "stopping X disturbed Y's jog bits"
        # every transition since Y started kept Y's bits intact
        assert all(w & Y_BITS == y_bits for w in fake.words[n_words:]), \
            f"a ControlWord transition touched Y: {fake.words[n_words:]}"
        dev.stop_all()
        assert fake._control == 0
    finally:
        dev.disconnect()


# ── config ───────────────────────────────────────────────────────────────
def test_config_roundtrip(tmp_path):
    cfg = TraverseConfig()
    cfg.loop_ms = 40
    cfg.y.enabled = False
    cfg.y.home_datum_in = -17.5
    cfg.home_backoff_margin_s = 0.4
    p = tmp_path / "traverse.json"
    cfg.save(p)
    back = TraverseConfig.load(p)
    assert back.ip == "192.168.1.21" and back.port == 502
    assert back.loop_ms == 40
    assert back.y.enabled is False
    assert back.z.fwd_increases_counts is False    # rig 2026-07-22 (settled)
    assert back.z.home_jog_fwd is True             # bit-level homing dir
    # limit config (rig 2026-07-22): X input disabled, active-low sense
    assert (back.x.limit_enabled, back.y.limit_enabled,
            back.z.limit_enabled) == (False, True, True)
    assert back.limit_active_low is True
    # soft limits ±18" for Y/Z (rig 2026-07-22)
    assert (back.y.min_in, back.y.max_in) == (-18.0, 18.0)
    assert (back.z.min_in, back.z.max_in) == (-25.0, 25.0)
    # all three axes: clean 1M rollover (reconfigured modules, 2026-07)
    assert (back.x.wrap_modulus, back.y.wrap_modulus,
            back.z.wrap_modulus) == (1_000_000,) * 3
    assert abs(back.x.clicks_per_inch - 13705.6) < 1e-6
    assert abs(back.y.clicks_per_inch + 14841.0) < 1e-6
    # Z slope POSITIVE per the rig 2026-07-22 sign convention
    # (+counts = DOWN = +inches; TOP = −18" datum)
    assert abs(back.z.clicks_per_inch - 986938.4) < 1e-6
    # host-side homing fields survive the round trip
    assert back.y.home_datum_in == -17.5
    assert back.home_backoff_margin_s == 0.4
    assert (back.x.home_enabled, back.y.home_enabled,
            back.z.home_enabled) == (False, True, True)
    # removed features STAY removed: no speed/accel/PLC-homing fields
    d = back.to_dict()
    for ax in ("x", "y", "z"):
        assert "speed_steps_s" not in d[ax]
        assert "accel" not in d[ax]
        assert "home_cmd_addr" not in d[ax]
    assert "motion_limits_supported" not in d
    assert "homing_supported" not in d
    assert "home_timeout_s" not in d


def test_from_dict_ignores_removed_keys_backward_compat(tmp_path):
    """Old saved JSONs still carry the retired speed/accel and
    PLC-homing fields — from_dict must IGNORE them, not crash."""
    legacy = TraverseConfig().to_dict()
    legacy["motion_limits_supported"] = True
    legacy["homing_supported"] = True
    legacy["home_timeout_s"] = 180.0
    for ax in ("x", "y", "z"):
        legacy[ax].update({
            "speed_addr": 12304, "accel_addr": 12305,
            "speed_steps_s": 1500, "accel": 300,
            "home_cmd_addr": 12310, "home_value": 25_000,
            "park_position": 24_000, "home_to_positive_limit": True,
            "home_seek_speed": 300, "home_backoff_speed": 150,
        })
    legacy["loop_ms"] = 35               # a real field still applies
    p = tmp_path / "legacy.json"
    p.write_text(json.dumps(legacy), encoding="utf-8")
    back = TraverseConfig.load(p)
    assert back.loop_ms == 35
    assert not hasattr(back, "homing_supported")
    assert not hasattr(back, "motion_limits_supported")
    assert not hasattr(back.x, "speed_steps_s")
    assert not hasattr(back.x, "home_cmd_addr")
    # and the re-serialized config is clean
    d = back.to_dict()
    assert "homing_supported" not in d and "speed_steps_s" not in d["x"]


def test_soft_limit_defaults_from_homing_datum():
    """Rig 2026-07-22 (settled): Y ±18\" with datum −18; Z ±25\" (for
    now) with datum +18 at its switch."""
    cfg = TraverseConfig()
    assert (cfg.y.min_in, cfg.y.max_in) == (-18.0, 18.0)
    assert (cfg.z.min_in, cfg.z.max_in) == (-25.0, 25.0)
    assert (cfg.y.home_datum_in, cfg.z.home_datum_in) == (-18.0, 18.0)
    assert cfg.x.home_enabled is False   # no homing on X
    # homing timeouts / margin defaults
    assert cfg.home_seek_timeout_s == 120.0
    assert cfg.home_backoff_timeout_s == 20.0
    assert cfg.home_backoff_margin_s == 0.25


# ── startup defaults ("Set as Defaults") ─────────────────────────────────
def test_defaults_path_env_override_and_startup_roundtrip(
        tmp_path, monkeypatch):
    from traverse_swt.config import defaults_path, load_startup_config
    p = tmp_path / "defaults.json"
    monkeypatch.setenv("TRAVERSE_DEFAULTS", str(p))
    assert defaults_path() == p
    # no file yet → factory defaults
    assert load_startup_config().loop_ms == TraverseConfig().loop_ms
    # save a customized config (the "Set as Defaults" path) → auto-load
    cfg = TraverseConfig()
    cfg.loop_ms = 77
    cfg.y.home_datum_in = -16.0
    cfg.z.calibrated = True              # live calibration state persists
    cfg.save(p)
    back = load_startup_config()
    assert back.loop_ms == 77
    assert back.y.home_datum_in == -16.0
    assert back.z.calibrated is True
    # corrupt file → guarded fall back to factory defaults, no crash
    p.write_text("{not json", encoding="utf-8")
    assert load_startup_config().loop_ms == TraverseConfig().loop_ms
