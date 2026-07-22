"""SSWT traverse HOST-side homing tests.

Homing is a host-side sequence now (no PLC registers): jog toward the
NEGATIVE limit watching the StatusWord bit (%MW1 bit0/1/2) → stop →
back off until the bit clears + a margin → calibrate_offset at the
datum. The emulator asserts the bit when the simulated axis reaches its
per-axis negative travel end, so the whole sequence runs end-to-end in
sim. No hardware required.
"""

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traverse_swt.config import TraverseConfig
from traverse_swt.device import TraverseDrive

FAST_SIM_RATE = 25_000.0     # counts/s (rig-realistic fixed rate: 2000)


def _sim_config(**kw) -> TraverseConfig:
    return TraverseConfig(force_sim=True, loop_ms=20, **kw)


def _connect_fast(dev: TraverseDrive, rate: float = FAST_SIM_RATE) -> None:
    dev.connect()
    dev._plc.sim_rate = rate


def _wait(cond, timeout=10.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return False


# ── happy path: seek → stop → backoff → calibrate_offset ─────────────────
def test_sim_home_y_completes_and_reads_datum_at_limit():
    cfg = _sim_config()
    msgs = []
    dev = TraverseDrive(cfg)
    dev.on_status = msgs.append
    try:
        _connect_fast(dev)
        assert dev.is_homed("y") is False
        assert cfg.y.calibrated is False           # power-up state
        sim_y = dev._plc._axes["Y"]

        res = dev.home_axis("y", wait=True)

        assert res.ok, f"sim home failed: {res}"
        assert res.state == "DONE" and res.fault == ""
        assert res.duration_s > 0
        assert dev.is_homed("y") is True
        st = dev.state()["Y"]
        assert st["homed"] is True and st["homing"] is False
        assert st["fault"] is None                 # homing owns the bit
        assert cfg.y.calibrated is True            # calibrate_offset ran
        # the CURRENT (backed-off) position reads the datum exactly…
        assert abs(st["inches"] - cfg.y.home_datum_in) < 0.01
        # …and the LIMIT position reads the datum to within the
        # backoff-margin travel (fixed sim rate × margin time) + slack
        margin_travel = (FAST_SIM_RATE * cfg.home_backoff_margin_s /
                         abs(cfg.y.clicks_per_inch))
        limit_inches = cfg.y.counts_to_inches(
            int(sim_y.neg_limit_counts))
        assert abs(limit_inches - cfg.y.home_datum_in) <= \
            margin_travel + 0.25, \
            f"limit reads {limit_inches:+.3f}\" vs datum " \
            f"{cfg.y.home_datum_in:+.3f}\" (margin {margin_travel:.3f}\")"
        # the axis is left stopped
        assert sim_y.cmd_rate == 0
        assert any("HOMED" in m for m in msgs)
    finally:
        dev.disconnect()


def test_sim_home_z_completes_with_real_slope():
    """Z homes with its real (huge) slope and settled config: the seek
    jogs the FWD bit (bit-level direction), and the datum at the switch
    reads +18.0 (the margin travel in inches is tiny)."""
    cfg = _sim_config()
    dev = TraverseDrive(cfg)
    try:
        # moderate sim rate: Z's per-axis rate scale (~66×, mirroring
        # the rig's slope-proportional counts rates) makes the default
        # FAST rate overshoot inches during the 0.25 s backoff margin —
        # keep the margin travel << the 0.1" assertion tolerance
        _connect_fast(dev, rate=2000)
        sim_z = dev._plc._axes["Z"]
        sim_z.neg_limit_counts = 987_938.0 * sim_z.seek_dir   # 1" out
        res = dev.home_axis("z", wait=True)
        assert res.ok and res.state == "DONE"
        assert dev.is_homed("z") is True
        assert cfg.z.calibrated is True
        st = dev.state()["Z"]
        assert abs(st["inches"] - 18.0) < 0.01
        limit_inches = cfg.z.counts_to_inches(
            int(sim_z.neg_limit_counts))
        assert abs(limit_inches - 18.0) < 0.1
    finally:
        dev.disconnect()


def test_homing_bit_is_flag_independent():
    """Rig 2026-07-22: homing and position mode need OPPOSITE direction
    senses. The homing ControlWord bit comes DIRECTLY from home_jog_fwd
    (seek = FWD bit for Z) and must not change even if the position-mode
    fwd_increases_counts flag is flipped mid-seek."""
    cfg = _sim_config()
    dev = TraverseDrive(cfg)
    try:
        _connect_fast(dev, rate=200)          # slow: catch SEEK in flight
        dev.home_axis("z", wait=False)
        assert _wait(lambda: (dev._plc.last_control() or 0)
                     & (cfg.z.fwd_mask | cfg.z.rev_mask), 5.0)
        w = dev._plc.last_control()
        assert w & cfg.z.fwd_mask, "Z seek must jog the FWD bit"
        assert not (w & cfg.z.rev_mask)
        # flip the position-mode flag mid-seek: the bookkeeping change
        # passes through the safe direction-change dwell (word 0), then
        # the homing bit comes back as FWD — never REV
        cfg.z.fwd_increases_counts = not cfg.z.fwd_increases_counts
        assert _wait(lambda: (dev._plc.last_control() or 0)
                     & (cfg.z.fwd_mask | cfg.z.rev_mask), 5.0), \
            "seek never resumed after the flag flip"
        w = dev._plc.last_control()
        assert w & cfg.z.fwd_mask and not (w & cfg.z.rev_mask), \
            "position-mode flag re-coupled into the homing direction"
        dev.abort_homing("z")
    finally:
        dev.disconnect()


def test_home_state_published_while_running():
    cfg = _sim_config()
    dev = TraverseDrive(cfg)
    try:
        _connect_fast(dev)
        res = dev.home_axis("y", wait=False)
        assert res.ok and res.state == "SEEK"
        assert dev.state()["Y"]["homing"] is True
        assert _wait(lambda: dev.state()["Y"]["home_state"] == "SEEK", 2.0)
        # a second homing while one runs is refused
        with pytest.raises(RuntimeError, match="already"):
            dev.home_axis("z")
        # …and so is a move
        cfg.x.calibrated = True
        with pytest.raises(RuntimeError, match="homing"):
            dev.move_to(x=0.5)
        assert _wait(lambda: dev.state()["Y"]["homed"], 15.0), \
            "homed never flipped in the published state"
        assert dev.state()["Y"]["home_state"] == ""
    finally:
        dev.disconnect()


# ── X: no homing ─────────────────────────────────────────────────────────
def test_x_homing_raises_clear_error():
    cfg = _sim_config()
    assert cfg.x.home_enabled is False
    dev = TraverseDrive(cfg)
    with pytest.raises(ValueError, match="no homing on X"):
        dev.home_axis("x")                 # even before connect
    try:
        _connect_fast(dev)
        with pytest.raises(ValueError, match="no homing on X"):
            dev.home_axis("x", wait=False)
        assert dev.state()["X"]["homing"] is False
    finally:
        dev.disconnect()


def test_home_requires_connection_and_idle_axes():
    cfg = _sim_config()
    dev = TraverseDrive(cfg)
    with pytest.raises(RuntimeError, match="connect"):
        dev.home_axis("y")
    try:
        _connect_fast(dev)
        dev._st["X"].moving = True         # any moving axis blocks homing
        with pytest.raises(RuntimeError, match="moving"):
            dev.home_axis("y")
        dev._st["X"].moving = False
    finally:
        dev.disconnect()


# ── aborts ───────────────────────────────────────────────────────────────
def test_abort_mid_seek_leaves_axis_stopped_not_homed():
    cfg = _sim_config()
    dev = TraverseDrive(cfg)
    try:
        dev.connect()                      # slow rig rate: seek is long
        res = dev.home_axis("y", wait=False)
        assert res.ok
        assert _wait(lambda: dev.state()["Y"]["home_state"] == "SEEK", 2.0)
        time.sleep(0.2)                    # well inside the seek
        dev.abort_homing("y")
        assert dev.state()["Y"]["homing"] is False
        result = dev._st["Y"].home_result
        assert result is not None and result.ok is False
        assert result.state == "ABORTED"
        assert dev.is_homed("y") is False
        assert cfg.y.calibrated is False
        assert _wait(lambda: dev._plc._axes["Y"].cmd_rate == 0, 2.0), \
            "axis still commanded after homing abort"
    finally:
        dev.disconnect()


def test_estop_and_per_axis_stop_abort_homing():
    cfg = _sim_config()
    dev = TraverseDrive(cfg)
    try:
        dev.connect()
        dev.home_axis("y", wait=False)
        time.sleep(0.1)
        dev.stop_all()                     # E-stop path
        assert dev.state()["Y"]["homing"] is False
        assert dev._st["Y"].home_result.state == "ABORTED"
        assert dev.is_homed("y") is False

        dev.home_axis("z", wait=False)
        time.sleep(0.1)
        dev.stop_axis("z")                 # per-axis stop path
        assert dev.state()["Z"]["homing"] is False
        assert dev._st["Z"].home_result.state == "ABORTED"
        assert dev.is_homed("z") is False
    finally:
        dev.disconnect()


def test_disconnect_releases_waiting_homer():
    cfg = _sim_config()
    dev = TraverseDrive(cfg)
    try:
        dev.connect()                      # slow: seek never finishes
        dev.home_axis("y", wait=False)
        time.sleep(0.1)
        assert dev.state()["Y"]["homing"] is True
    finally:
        dev.disconnect()
    assert dev.state()["Y"]["homing"] is False
    assert dev._st["Y"].home_result.state in ("ABORTED", "DISCONNECTED")
    assert dev.is_homed("y") is False


# ── timeouts fault cleanly ───────────────────────────────────────────────
def test_seek_timeout_faults_cleanly():
    cfg = _sim_config()
    cfg.home_seek_timeout_s = 0.4          # switch unreachable at 2000/s
    msgs = []
    dev = TraverseDrive(cfg)
    dev.on_status = msgs.append
    try:
        dev.connect()                      # slow rig rate on purpose
        res = dev.home_axis("y", wait=True)
        assert res.ok is False
        assert res.state == "SEEK_TIMEOUT"
        assert res.fault == "SEEK_TIMEOUT"
        assert dev.state()["Y"]["homing"] is False
        assert dev.is_homed("y") is False
        assert cfg.y.calibrated is False
        assert _wait(lambda: dev._plc._axes["Y"].cmd_rate == 0, 2.0), \
            "axis still commanded after seek timeout"
        assert any("FAILED" in m for m in msgs)
    finally:
        dev.disconnect()


def test_backoff_timeout_faults_cleanly():
    cfg = _sim_config()
    cfg.home_backoff_timeout_s = 0.5
    dev = TraverseDrive(cfg)
    try:
        _connect_fast(dev)
        # a switch that NEVER releases: the bit is set everywhere, so
        # the seek "finds" it immediately and the backoff can't clear it
        sim_y = dev._plc._axes["Y"]
        sim_y.neg_limit_counts = -1e12 * sim_y.neg_dir
        res = dev.home_axis("y", wait=True)
        assert res.ok is False
        assert res.state == "BACKOFF_TIMEOUT"
        assert dev.is_homed("y") is False
        assert dev.state()["Y"]["homing"] is False
        assert _wait(lambda: sim_y.cmd_rate == 0, 2.0)
    finally:
        dev.disconnect()


# ── homing is per-power-cycle / re-homable ───────────────────────────────
def test_rehoming_after_failed_cycle_succeeds():
    cfg = _sim_config()
    cfg.home_seek_timeout_s = 0.3
    dev = TraverseDrive(cfg)
    try:
        dev.connect()                      # slow: first cycle times out
        res = dev.home_axis("y", wait=True)
        assert res.ok is False and dev.is_homed("y") is False
        cfg.home_seek_timeout_s = 120.0
        dev._plc.sim_rate = FAST_SIM_RATE  # now the switch is reachable
        res2 = dev.home_axis("y", wait=True)
        assert res2.ok is True
        assert dev.is_homed("y") is True
    finally:
        dev.disconnect()
