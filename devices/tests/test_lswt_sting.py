"""LSWT Sting driver tests — config math, wire protocol against the
emulator, sim device lifecycle, fault/watchdog paths, and byte-accuracy
of the command stream vs. the legacy C# tool. No hardware required.
"""

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import lswt_sting.device as sting_device
from lswt_sting.config import (ALPHA_STEPS_PER_DEG, BETA_STEPS_PER_DEG,
                               StingAxisConfig, StingConfig)
from lswt_sting.device import StingDrive
from lswt_sting.emulator import SimSerial
from lswt_sting.protocol import (BUSY, READY, STALLED, StingError,
                                 StingProtocol)


# ── helpers ──────────────────────────────────────────────────────────────
def _sim_config(**kw) -> StingConfig:
    """Fast sim config (no Z-reset 1.1 s sleep, quick poll). Park and
    position-restore are opt-in per test: parking is a slow blocking
    move, and restore/save must not touch the real state file."""
    kw.setdefault("park_on_disconnect", False)
    kw.setdefault("restore_position", False)
    kw.setdefault("state_path", str(Path(tempfile.gettempdir())
                                    / "sting_state_test.json"))
    return StingConfig(force_sim=True, poll_ms=50, init_reset=False, **kw)


def _wait_until(pred, timeout=10.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _wait_settled(dev, timeout=15.0):
    return _wait_until(lambda: not dev.moving, timeout)


class _DeadPort:
    """Port that never answers (read timeout → empty bytes)."""

    def write(self, data):
        return len(data)

    def read_until(self, expected=b"\r"):
        return b""

    def reset_input_buffer(self):
        pass

    def close(self):
        pass


class _BadEchoPort(_DeadPort):
    """Port that echoes garbage for every command."""

    def read_until(self, expected=b"\r"):
        return b"XX\r"


class _RecordingSim(SimSerial):
    """SimSerial that records every command line written to the wire."""

    def __init__(self, config=None):
        super().__init__(config)
        self.sent = []

    def write(self, data):
        self.sent.append(data.rstrip(b"\r\n").decode("ascii"))
        return super().write(data)


def _assert_subsequence(sent, expected):
    """All ``expected`` lines appear in ``sent`` in order (gaps allowed)."""
    idx = 0
    for exp in expected:
        try:
            idx = sent.index(exp, idx) + 1
        except ValueError:
            raise AssertionError(
                f"{exp!r} not found (in order) in wire log: {sent}")


# ── 1. config ────────────────────────────────────────────────────────────
def test_counts_angle_roundtrip():
    ax = StingAxisConfig(zero_offset_deg=3.25)
    assert abs(ax.counts_to_angle(0) - 3.25) < 1e-12
    assert ax.angle_to_counts(3.25) == 0
    # legacy sign (Core.exe IL): +1 degree = MINUS steps_per_degree
    assert ax.angle_to_counts(4.25) == -round(ALPHA_STEPS_PER_DEG)
    for counts in (0, 1, -1, 2741, -99_999, 123_456):
        assert ax.angle_to_counts(ax.counts_to_angle(counts)) == counts
    beta = StingAxisConfig(name="Beta", unit="2",
                           steps_per_degree=BETA_STEPS_PER_DEG,
                           zero_offset_deg=-2.0)
    assert abs(beta.counts_to_angle(668) - (-2.0 - 668 / 66.8)) < 1e-4
    assert beta.angle_to_counts(beta.counts_to_angle(-500)) == -500


def test_velocity_estimate():
    cfg = StingConfig()
    # alpha: 0.108 rev/s × 25000 steps/rev / 2741.0525 steps/deg ≈ 0.985
    v_a = cfg.alpha.velocity_deg_s()
    assert abs(v_a - 0.108 * 25_000 / ALPHA_STEPS_PER_DEG) < 1e-9
    assert abs(v_a - 0.985) < 1e-3
    # beta: 0.032 × 25000 / 66.8 ≈ 11.98 deg/s (fast axis)
    v_b = cfg.beta.velocity_deg_s()
    assert abs(v_b - 0.032 * 25_000 / BETA_STEPS_PER_DEG) < 1e-9
    assert abs(v_b - 11.976) < 1e-2
    # unparsable velocity string falls back to 0.1 rev/s
    bad = StingAxisConfig(velocity="fast")
    assert abs(bad.velocity_deg_s() - 0.1 * 25_000 / ALPHA_STEPS_PER_DEG) \
        < 1e-9


def test_config_json_roundtrip():
    cfg = StingConfig()
    cfg.poll_ms = 99
    cfg.com_port = "COM7"
    cfg.alpha.zero_offset_deg = 1.5
    cfg.alpha.zeroed = True
    cfg.beta.min_deg = -5.0
    cfg.beta.enabled = False
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "sting.json"
        cfg.save(p)
        back = StingConfig.load(p)
    assert back.poll_ms == 99
    assert back.com_port == "COM7"
    assert back.alpha.zero_offset_deg == 1.5
    assert back.alpha.zeroed is True
    assert back.beta.min_deg == -5.0
    assert back.beta.enabled is False
    # legacy motion parameters survive as the exact on-wire strings
    assert back.alpha.acceleration == "10.8528"
    assert back.alpha.velocity == ".108"
    assert back.beta.velocity == ".032"
    assert abs(back.alpha.steps_per_degree - ALPHA_STEPS_PER_DEG) < 1e-9
    assert abs(back.beta.steps_per_degree - BETA_STEPS_PER_DEG) < 1e-9


def test_config_unknown_keys_tolerated():
    d = StingConfig().to_dict()
    d["future_top_level_option"] = 42
    d["alpha"]["mystery_axis_field"] = "??"
    d["alpha"]["zero_offset_deg"] = 7.0
    back = StingConfig.from_dict(d)
    assert back.alpha.zero_offset_deg == 7.0
    assert back.beta.unit == "2"
    # missing axis dict falls back to the per-axis defaults
    d2 = {"poll_ms": 123}
    back2 = StingConfig.from_dict(d2)
    assert back2.poll_ms == 123
    assert abs(back2.alpha.steps_per_degree - ALPHA_STEPS_PER_DEG) < 1e-9


def test_enabled_axes():
    cfg = StingConfig()
    assert [a.name for a in cfg.enabled_axes()] == ["Alpha", "Beta"]
    cfg.beta.enabled = False
    assert [a.name for a in cfg.enabled_axes()] == ["Alpha"]
    assert [a.name for a in cfg.axes()] == ["Alpha", "Beta"]


# ── 2. protocol vs SimSerial (wire level, no device) ─────────────────────
def test_protocol_echo_validation():
    p = StingProtocol(SimSerial(StingConfig()))
    p.command("1", "PZ")                  # good echo passes silently
    bad = StingProtocol(_BadEchoPort())
    try:
        bad.command("1", "G")
        raise AssertionError("bad echo not rejected")
    except StingError:
        pass


def test_protocol_status_mapping():
    sim = SimSerial(StingConfig())
    p = StingProtocol(sim)
    assert p.status("1") == READY
    p.move_steps("1", 50_000)             # long move → busy
    assert p.status("1") == BUSY
    sim.inject_stall = "1"
    assert p.status("1") == STALLED
    sim.inject_stall = None
    p.stop_all_now(["1"])


def test_protocol_position_signed():
    sim = SimSerial(StingConfig())
    p = StingProtocol(sim)
    assert p.position("1") == 0
    sim._axes["1"].counts = -12_345.0
    assert p.position("1") == -12_345
    sim._axes["2"].counts = 678.0
    assert p.position("2") == 678


def test_protocol_move_and_zero():
    sim = SimSerial(StingConfig())
    p = StingProtocol(sim)
    p.move_steps("1", 50_000)             # D then G
    assert p.status("1") == BUSY
    time.sleep(0.15)                      # let the sim integrate motion
    p.stop_all_now(["1"])
    moved = p.position("1")
    assert moved > 0, "sim axis did not advance during the move"
    p.zero_position("1")
    assert p.position("1") == 0


def test_protocol_stop_all_drains_echoes():
    sim = SimSerial(StingConfig())
    p = StingProtocol(sim)
    p.move_steps("1", 50_000)
    p.move_steps("2", 50_000)
    p.stop_all_now(["1", "2"])            # raw writes + drain
    # a following query must not read the stop echoes as its response
    assert p.status("1") == READY
    assert p.status("2") == READY


def test_protocol_timeout_raises():
    p = StingProtocol(_DeadPort())
    try:
        p.status("1")
        raise AssertionError("empty read did not raise")
    except StingError as exc:
        assert "no response" in str(exc)
    closed = StingProtocol(None)
    try:
        closed.query("1", "R")
        raise AssertionError("unopened port not rejected")
    except StingError:
        pass


# ── 3. device sim lifecycle ──────────────────────────────────────────────
def test_connect_and_state_shape():
    dev = StingDrive(_sim_config())
    try:
        dev.connect()
        assert dev.connected and dev.sim_mode
        assert dev.fault is None
        st = dev.state()
        assert set(st) == {"Alpha", "Beta", "fault"}
        for name in ("Alpha", "Beta"):
            assert set(st[name]) == {"angle", "counts", "moving", "target",
                                     "zeroed", "enabled", "responding"}
            assert st[name]["responding"]
            assert not st[name]["moving"]
    finally:
        dev.disconnect()
    assert not dev.connected


def test_move_to_refused_before_zero():
    dev = StingDrive(_sim_config())
    try:
        dev.connect()
        try:
            dev.move_to(alpha=1.0)
            raise AssertionError("unzeroed absolute move not rejected")
        except ValueError:
            pass
        assert not dev.moving
    finally:
        dev.disconnect()


def test_move_by_allowed_unzeroed():
    dev = StingDrive(_sim_config())
    try:
        dev.connect()
        assert not dev.state()["Alpha"]["zeroed"]
        dev.move_by("alpha", 0.01)        # jog: -27 steps (legacy sign)
        assert _wait_settled(dev), "jog never completed"
        assert dev.state()["Alpha"]["counts"] == \
            -round(0.01 * ALPHA_STEPS_PER_DEG)
    finally:
        dev.disconnect()


def test_set_current_angle_zeroes():
    dev = StingDrive(_sim_config())
    try:
        dev.connect()
        dev.set_current_angle("beta", 2.5)
        st = dev.state()["Beta"]
        assert st["zeroed"]
        assert st["counts"] == 0
        assert abs(st["angle"] - 2.5) < 1e-9
        assert dev.config.beta.zero_offset_deg == 2.5
    finally:
        dev.disconnect()


def test_move_to_completes_and_callback():
    dev = StingDrive(_sim_config())
    completed = []
    try:
        dev.connect()
        dev.on_move_complete = completed.append
        dev.set_current_angle("beta", 0.0)
        dev.move_to(beta=3.0)             # beta ≈ 12 deg/s → ~0.25 s
        assert dev.state()["Beta"]["target"] == 3.0
        assert _wait_settled(dev), "beta move never completed"
        st = dev.state()["Beta"]
        assert abs(st["angle"] - 3.0) < 0.1
        assert st["counts"] == dev.config.beta.angle_to_counts(3.0)
        assert st["counts"] > 0     # beta: +angle = +counts (field 7/22)
        assert _wait_until(lambda: "Beta" in completed, 2.0), \
            "on_move_complete never fired"
    finally:
        dev.disconnect()


def test_limits_rejected_before_motion():
    dev = StingDrive(_sim_config())
    try:
        dev.connect()
        dev.set_current_angle("alpha", 0.0)
        dev.set_current_angle("beta", 0.0)
        # one bad target must veto the WHOLE request before any command
        try:
            dev.move_to(alpha=0.5, beta=999.0)
            raise AssertionError("limit violation not rejected")
        except ValueError:
            pass
        st = dev.state()
        assert st["Alpha"]["counts"] == 0 and st["Beta"]["counts"] == 0
        assert not st["Alpha"]["moving"] and not st["Beta"]["moving"]
        time.sleep(0.2)                   # no delayed motion either
        assert dev.state()["Alpha"]["counts"] == 0
    finally:
        dev.disconnect()


def test_stop_all_halts_motion():
    dev = StingDrive(_sim_config())
    try:
        dev.connect()
        dev.set_current_angle("beta", 0.0)
        dev.move_to(beta=10.0)            # ~0.85 s at sim speed
        time.sleep(0.25)
        assert dev.state()["Beta"]["moving"]
        dev.stop_all()
        assert not dev.moving
        time.sleep(0.15)                  # let the poll tick past the stop
        c1 = dev.state()["Beta"]["counts"]
        time.sleep(0.3)
        c2 = dev.state()["Beta"]["counts"]
        assert abs(c2 - c1) <= 2, "axis kept moving after stop_all"
        # heading toward +668 counts (+10 deg, beta dir=+1), stopped short
        assert 0 < c1 < round(10.0 * BETA_STEPS_PER_DEG)
    finally:
        dev.disconnect()


def test_double_move_rejected():
    dev = StingDrive(_sim_config())
    try:
        dev.connect()
        dev.set_current_angle("beta", 0.0)
        dev.move_to(beta=10.0)
        try:
            dev.move_to(beta=5.0)
            raise AssertionError("double move not rejected")
        except RuntimeError:
            pass
        dev.stop_all()
    finally:
        dev.disconnect()


# ── 4. faults ────────────────────────────────────────────────────────────
def test_stall_latches_fault_and_reset():
    dev = StingDrive(_sim_config())
    try:
        dev.connect()
        dev.set_current_angle("beta", 0.0)
        dev.move_to(beta=10.0)
        time.sleep(0.15)
        dev._proto._sp.inject_stall = "2"
        assert _wait_until(lambda: dev.fault is not None, 5.0), \
            "stall never latched a fault"
        assert "STALL" in dev.fault
        assert not dev.moving
        try:
            dev.move_to(beta=1.0)
            raise AssertionError("motion allowed while fault latched")
        except RuntimeError:
            pass
        dev._proto._sp.inject_stall = None
        dev.reset_fault()
        assert dev.fault is None
        # open loop: let the poll re-sync the host counter with the
        # indexer before commanding an absolute move again
        sim = dev._proto._sp
        assert _wait_until(
            lambda: dev.state()["Beta"]["counts"] == sim.axis_counts("2"),
            5.0), "host counter never re-synced after the fault"
        dev.move_to(beta=1.0)             # motion works again after reset
        assert _wait_settled(dev), "post-reset move never completed"
        assert abs(dev.state()["Beta"]["angle"] - 1.0) < 0.1
    finally:
        dev.disconnect()


def test_move_timeout_latches_fault():
    dev = StingDrive(_sim_config())
    try:
        dev.connect()
        dev.set_current_angle("beta", 0.0)
        dev.move_to(beta=10.0)
        assert dev.state()["Beta"]["moving"]
        dev._beta.deadline = 0.0          # force the deadline into the past
        assert _wait_until(lambda: dev.fault is not None, 5.0), \
            "timeout never latched a fault"
        assert "TIMED OUT" in dev.fault
        assert not dev.moving
        dev.reset_fault()
        assert dev.fault is None
    finally:
        dev.disconnect()


def test_reinitialize_requires_confirm_and_clears_zero():
    dev = StingDrive(_sim_config())
    try:
        dev.connect()
        dev.set_current_angle("alpha", 0.0)
        dev.set_current_angle("beta", 0.0)
        assert dev.config.alpha.zeroed and dev.config.beta.zeroed
        try:
            dev.reinitialize()
            raise AssertionError("reinitialize without confirm_safe")
        except RuntimeError:
            pass
        assert dev.config.alpha.zeroed    # refused call changed nothing
        dev.reinitialize(confirm_safe=True)
        assert not dev.config.alpha.zeroed and not dev.config.beta.zeroed
        try:
            dev.move_to(alpha=1.0)        # absolute moves refused again
            raise AssertionError("move allowed after re-init un-zeroed")
        except ValueError:
            pass
    finally:
        dev.disconnect()


# ── 5. serial watchdog ───────────────────────────────────────────────────
def test_serial_watchdog_trips():
    cfg = _sim_config()
    cfg.max_consecutive_errors = 3
    dev = StingDrive(cfg)
    msgs = []
    try:
        dev.connect()
        dev.on_status = msgs.append
        dev._proto._sp = _DeadPort()      # every read now times out
        assert _wait_until(lambda: dev.fault is not None, 5.0), \
            "watchdog never tripped"
        assert "watchdog" in dev.fault.lower()
        assert not dev.moving
        assert any("WATCHDOG" in m for m in msgs), \
            f"no watchdog status message: {msgs}"
        assert sum("Serial error" in m for m in msgs) >= \
            cfg.max_consecutive_errors
    finally:
        dev.disconnect()


# ── 6. command byte-accuracy vs the legacy tool ──────────────────────────
def test_init_sequence_bytes_match_legacy():
    """The full InitHw command stream, byte-for-byte, in legacy order
    (init_reset=True explicitly — default is now OFF — so the Z reset
    is included)."""
    cfg = StingConfig(force_sim=True, poll_ms=50, init_reset=True,
                      park_on_disconnect=False, restore_position=False,
                      state_path=str(Path(tempfile.gettempdir())
                                     / "sting_state_test.json"))
    created = []

    def factory(c):
        s = _RecordingSim(c)
        created.append(s)
        return s

    orig = sting_device.SimSerial
    sting_device.SimSerial = factory
    dev = StingDrive(cfg)
    try:
        dev.connect()
        sim = created[0]
        _assert_subsequence(sim.sent, [
            "1R",                         # chain probe
            "SSI1",                       # broadcast interface setup
            "1SSA0", "1Z",                # alpha setup + reset
            "2SSA0", "2Z",                # beta setup + reset
            "1R", "2R",                   # wait-for-READY after reset
            "1LD3", "2LD3",               # disable limit inputs
            "1PR",                        # alpha position readback
            "1A10.8528", "1AD10.8528", "1V.108",
            "2PR",                        # beta position readback
            "2A2", "2AD2", "2V.032",
            "FSD1",                       # broadcast final setup
        ])
        # a zeroed alpha absolute move loads D in steps then G.
        # Sign per the deployed C# (Core.exe SetStingToAngle IL):
        # counts = angle x -2741.0525 → +1.0° loads D-2741.
        dev.set_current_angle("alpha", 0.0)
        assert "1PZ" in sim.sent
        n0 = len(sim.sent)
        dev.move_to(alpha=1.0)
        tail = sim.sent[n0:]
        assert "1D-2741" in tail, f"no 1D-2741 in {tail}"
        assert "1G" in tail[tail.index("1D-2741") + 1:], \
            "G did not follow the distance load"
        dev.stop_all()
    finally:
        dev.disconnect()
        sting_device.SimSerial = orig


# ── startup defaults (house defaults_path pattern) ───────────────────────
def test_defaults_path_env_override_and_startup_roundtrip(
        tmp_path, monkeypatch):
    from lswt_sting.config import defaults_path, load_startup_config
    p = tmp_path / "defaults.json"
    monkeypatch.setenv("LSWT_STING_DEFAULTS", str(p))
    assert defaults_path() == p
    # absent file → factory defaults
    assert load_startup_config().com_port == StingConfig().com_port
    cfg = StingConfig()
    cfg.com_port = "COM7"
    cfg.poll_ms = 123
    cfg.save(p)
    back = load_startup_config()
    assert back.com_port == "COM7"
    assert back.poll_ms == 123


def test_load_startup_config_corrupt_falls_back(tmp_path, monkeypatch):
    from lswt_sting.config import load_startup_config
    p = tmp_path / "defaults.json"
    monkeypatch.setenv("LSWT_STING_DEFAULTS", str(p))
    p.write_text("{not json", encoding="utf-8")
    assert load_startup_config().com_port == StingConfig().com_port


def _run_all():
    import inspect
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)
           and not inspect.signature(v).parameters]   # fixture tests: pytest
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} sting tests passed.")


if __name__ == "__main__":
    _run_all()
