"""Three-tier tunnel-speed control (config.tunnel_control_mode).

Covers every tier end-to-end in sim + fakes that record set_target:

* MANUAL — monitor-only, never writes the fan (verify-on prompts, verify-off
  records immediately);
* AUTO — commands the adapter's NATIVE kwarg ONCE (LSWT → hz=/velocity=,
  SWT → rpm=), waits the drive settle, records; NO measured-feedback, NO
  fault even with a garbage/None measured value;
* REGULATE — AUTO + a measured-feedback loop in the selected unit; on
  non-convergence WARNS + RECORDS by default, FAULTS only with
  tunnel_regulate_fault=True;
* the AIR-OFF short-circuit (Casey's Hz-0 repro) — a 0 target commands the
  fan to 0 and records, never regulating a garbage measured Mach toward 0;
* the Hz end-to-end path (30 Hz → the LSWT drive commanded 30, not 0.5);
* config round-trip + legacy-boolean derivation both directions.

No hardware, offscreen Qt only where the dialog is exercised.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List

import h5py
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freestream._fakes import FakeStreamer
from freestream.config import FreestreamConfig
from freestream.derived import GAMMA, tunnel_state
from freestream.hal import OK, ChannelSpec, DeviceStatus
from freestream.manager import DeviceManager
from freestream.recorder import Hdf5Recorder
from freestream.runsheet import SweepPoint
from freestream.sweep import DONE, FAILED, SweepCallbacks, SweepEngine

PTOT_PSIA = 11.38
TEMP_C = 21.0


def pdiff_for_mach(mach: float, ptot: float = PTOT_PSIA) -> float:
    """The Pdiff [psi] that reads as *mach* through the isentropic chain."""
    ratio = (1.0 + 0.5 * (GAMMA - 1.0) * mach ** 2) ** (GAMMA / (GAMMA - 1.0))
    return ptot - ptot / ratio


# ── recording setpoint fakes ─────────────────────────────────────────────
class _RecordingTunnelBase:
    """SetpointDevice stand-in that RECORDS every set_target(**kw); instant
    ramp with a tiny delay. Subclasses pick the native readback shape."""

    def __init__(self, sim: bool = False, **limits):
        self.id = "tun"
        self.label = "Recording Tunnel"
        self._sim = sim
        self._connected = True
        self.calls: List[Dict[str, float]] = []
        self.config = SimpleNamespace(**limits)
        self._native = 0.0            # the last commanded native value
        self._target = 0.0
        self._at = 0.0

    def connect(self):
        self._connected = True

    def disconnect(self):
        self._connected = False

    @property
    def connected(self):
        return self._connected

    @property
    def sim(self):
        return self._sim

    def status(self):
        return DeviceStatus(state=OK, sim=self._sim, last_sample_age_s=0.0)

    def at_target(self):
        if time.perf_counter() >= self._at:
            self._native = self._target
            return True
        return False


class RecordingSwtTunnel(_RecordingTunnelBase):
    """SWT-style: readback carries rpm/rpm_set → the engine commands rpm=."""

    def set_target(self, **kw):
        self.calls.append(dict(kw))
        if "rpm" not in kw:
            raise ValueError(f"swt setpoint is rpm=; got {sorted(kw)}")
        self._target = float(kw["rpm"])
        self._at = time.perf_counter() + 0.05

    def readback(self):
        return {"rpm": self._native, "rpm_set": self._target}


class RecordingLswtTunnel(_RecordingTunnelBase):
    """LSWT-style: readback carries hz/velocity_fps → the engine commands
    hz= (or velocity=). rpm≡hz 1:1 like the real ABB ACS530 adapter."""

    def set_target(self, **kw):
        self.calls.append(dict(kw))
        keys = set(kw) & {"hz", "velocity", "rpm"}
        if len(keys) != 1:
            raise ValueError(f"lswt setpoint is one of hz/velocity/rpm; "
                             f"got {sorted(kw)}")
        if "velocity" in kw:                 # ft/s → a pretend hz (÷2)
            self._target = float(kw["velocity"]) / 2.0
        else:
            self._target = float(kw.get("hz", kw.get("rpm", 0.0)))
        self._at = time.perf_counter() + 0.05

    def readback(self):
        return {"hz": self._native, "hz_set": self._target,
                "velocity_fps": self._native * 2.0,
                "rpm": self._native, "rpm_set": self._target}


class RecordingLswtFanTunnel(RecordingLswtTunnel):
    """LSWT-style + the ABB fan lifecycle: ``fan_start()`` and a
    ``snapshot().fan_running`` indicator, like the real LswtTunnelAdapter.
    ``arms`` picks whether fan_start actually makes the drive report
    running (False models a drive whose fan control is not enabled — the
    reference is set but the fan never spools)."""

    def __init__(self, *a, arms: bool = True, **kw):
        super().__init__(*a, **kw)
        self._running = False
        self._arms = bool(arms)
        self.fan_start_calls = 0
        self.fan_stop_calls = 0

    def fan_start(self):
        self.fan_start_calls += 1
        if self._arms:
            self._running = True

    def fan_stop(self):
        self.fan_stop_calls += 1
        self._running = False

    def snapshot(self):
        return SimpleNamespace(fan_running=self._running)


class FixedMachDaq(FakeStreamer):
    """Tunnel-condition stream pinned to a FIXED measured Mach — models
    Casey's OPEN Pdiff channel (a garbage reading the fan can't regulate)."""

    def __init__(self, mach: float = 0.137, sim: bool = False):
        super().__init__(sim=sim, rate=200.0, group="DaqBook2005",
                         channels=("Pdiff", "Ptot", "Temp"))
        self.id = "daq"
        self.label = "Fixed-Mach DAQ"
        self._vals = {"Pdiff": pdiff_for_mach(mach), "Ptot": PTOT_PSIA,
                      "Temp": TEMP_C}

    def latest(self):
        return dict(self._vals)

    def drain_block(self):
        import numpy as np
        block = FakeStreamer.drain_block(self)
        return {c: np.full(len(a), self._vals[c]) for c, a in block.items()}


FAKES = {
    "balance": {"adapter": "freestream._fakes.FakeStreamer", "enabled": True},
    "daq": {"adapter": "freestream._fakes.FakeDaq", "enabled": True},
    "pos": {"adapter": "freestream._fakes.FakePositioner", "enabled": True},
    "tun": {"adapter": "freestream._fakes.FakeTunnel", "enabled": True},
}
MODES = {"mode1": {"positioner": "pos", "balance": "balance",
                   "tunnel_conditions": "daq", "tunnel": "tun"}}


def _rig(tmp_path, tunnel=None, daq=None, **cfg_kw):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"modes": MODES, "devices": FAKES}),
                        encoding="utf-8")
    mgr = DeviceManager("mode1", sim=True, manifest_path=manifest)
    mgr.connect_all()
    if tunnel is not None:
        mgr.devices["tun"] = tunnel
        tunnel.connect()
    if daq is not None:
        mgr.devices["daq"] = daq
        daq.connect()
    for s in mgr.streaming:
        s.start()
    defaults = dict(samples=50, dwell_s=0.05, move_timeout_s=5,
                    tunnel_timeout_s=5, operator="pytest")
    defaults.update(cfg_kw)
    cfg = FreestreamConfig(**defaults)
    rec = Hdf5Recorder(tmp_path / "runs", config_name="tiertest")
    return mgr, rec, cfg


def _mach_point(mach=0.3, **meta):
    return SweepPoint(alpha=0.0, mach=mach, dwell_s=0.05, samples=50,
                      meta=meta)


# ═══ config: mode + legacy-boolean derivation ════════════════════════════
def test_default_mode_is_manual():
    cfg = FreestreamConfig()
    assert cfg.tunnel_control_mode == "manual"
    assert cfg.tunnel_control_enabled is False
    assert cfg.tunnel_regulate_fault is False


def test_legacy_enabled_true_derives_regulate():
    # code building the OLD boolean gets the old closed-loop intent
    cfg = FreestreamConfig(tunnel_control_enabled=True)
    assert cfg.tunnel_control_mode == "regulate"


def test_from_dict_derivation_both_directions(tmp_path):
    # key absent + legacy True → regulate
    p = tmp_path / "a.json"
    p.write_text(json.dumps({"tunnel_control_enabled": True}),
                 encoding="utf-8")
    assert FreestreamConfig.load(p).tunnel_control_mode == "regulate"
    # key absent + legacy False (or missing) → manual
    p.write_text(json.dumps({"operator": "casey"}), encoding="utf-8")
    assert FreestreamConfig.load(p).tunnel_control_mode == "manual"


def test_explicit_mode_round_trips(tmp_path):
    cfg = FreestreamConfig(tunnel_control_mode="auto",
                           tunnel_regulate_fault=True)
    assert cfg.tunnel_control_mode == "auto"
    p = tmp_path / "cfg.json"
    cfg.save(p)
    back = FreestreamConfig.load(p)
    assert back.tunnel_control_mode == "auto"
    assert back.tunnel_regulate_fault is True


def test_unknown_mode_falls_back_to_manual():
    assert FreestreamConfig(
        tunnel_control_mode="warp").tunnel_control_mode == "manual"


# ═══ Tier A: MANUAL (monitor-only) ═══════════════════════════════════════
def test_manual_verify_on_never_writes_the_fan(tmp_path):
    tun = RecordingSwtTunnel(sim=True, rpm_max=2000.0)
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun)     # default = manual+verify
    assert cfg.tunnel_control_mode == "manual"
    waits = []
    engine = SweepEngine(mgr, rec, cfg, SweepCallbacks(
        on_operator_wait=lambda r: waits.append(r) or "proceed"))
    out = engine.run([_mach_point(0.3)])[0]
    assert out.status == DONE
    assert len(waits) == 1                         # verify prompted
    assert tun.calls == []                         # fan NEVER commanded


def test_manual_verify_off_records_immediately(tmp_path):
    tun = RecordingSwtTunnel(sim=True, rpm_max=2000.0)
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, mach_check_enabled=False)
    waits, events = [], []
    engine = SweepEngine(mgr, rec, cfg, SweepCallbacks(
        on_event=events.append,
        on_operator_wait=lambda r: waits.append(r) or "proceed"))
    out = engine.run([_mach_point(0.3)])[0]
    assert out.status == DONE
    assert waits == []                             # no dialog
    assert tun.calls == []                         # still no write
    assert any("verification disabled" in e for e in events)


# ═══ Tier B: AUTO (open-loop native command) ═════════════════════════════
def test_auto_swt_commands_rpm_once_no_feedback(tmp_path):
    tun = RecordingSwtTunnel(sim=False, rpm_max=2000.0)
    daq = FixedMachDaq(mach=0.137, sim=False)      # garbage measured value
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, daq=daq,
                         tunnel_control_mode="auto")
    events = []
    engine = SweepEngine(mgr, rec, cfg,
                         SweepCallbacks(on_event=events.append))
    out = engine.run([_mach_point(0.3)])[0]
    assert out.status == DONE                       # NO fault on the garbage
    # SWT native kwarg is rpm=, commanded exactly once (no feedback loop)
    assert tun.calls == [{"rpm": pytest.approx(0.3 * cfg.rpm_per_mach)}]
    assert not any("tunnel regulate" in e for e in events)


def test_auto_lswt_commands_hz_from_hz_unit(tmp_path):
    # Casey's Hz end-to-end: speed_unit=hz, a 30-Hz point → the LSWT drive
    # commanded 30 Hz VERBATIM (not 0.5), recorded.
    from freestream import speed
    tun = RecordingLswtTunnel(sim=False, max_hz=60.0)
    daq = FixedMachDaq(mach=0.137, sim=False)
    mach30 = speed.mach_from(30.0, "hz")
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, daq=daq,
                         tunnel_control_mode="auto", speed_unit="hz")
    engine = SweepEngine(mgr, rec, cfg)
    pt = SweepPoint(alpha=0.0, mach=mach30, dwell_s=0.05, samples=50,
                    meta={"speed_value": 30.0, "speed_unit": "hz"})
    out = engine.run([pt])[0]
    assert out.status == DONE
    assert tun.calls == [{"hz": pytest.approx(30.0)}]   # NOT 0.5, NOT ×60
    with h5py.File(out.path, "r") as f:
        # native command recorded (Hz≡RPM 1:1) + canonical Mach
        assert f["Tunnel/RPM_cmd"][0] == pytest.approx(30.0)
        assert f["Tunnel/Mach_cmd"][0] == pytest.approx(mach30)


def test_auto_lswt_velocity_unit_commands_velocity(tmp_path):
    tun = RecordingLswtTunnel(sim=False, max_hz=60.0)
    daq = FixedMachDaq(mach=0.05, sim=False)
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, daq=daq,
                         tunnel_control_mode="auto", speed_unit="ft/s")
    engine = SweepEngine(mgr, rec, cfg)
    from freestream import speed
    mach = speed.mach_from(80.0, "ft/s")
    pt = SweepPoint(alpha=0.0, mach=mach, dwell_s=0.05, samples=50,
                    meta={"speed_value": 80.0, "speed_unit": "ft/s"})
    out = engine.run([pt])[0]
    assert out.status == DONE
    assert tun.calls == [{"velocity": pytest.approx(80.0)}]  # entered fps


# ═══ Tier C: REGULATE (closed-loop to tolerance) ═════════════════════════
def test_regulate_converges_records(tmp_path):
    # a DAQ that reads the truth (Mach == target) → first command lands
    tun = RecordingSwtTunnel(sim=False, rpm_max=2000.0)
    daq = FixedMachDaq(mach=0.3, sim=False)
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, daq=daq,
                         tunnel_control_mode="regulate")
    events = []
    engine = SweepEngine(mgr, rec, cfg,
                         SweepCallbacks(on_event=events.append))
    out = engine.run([_mach_point(0.3)])[0]
    assert out.status == DONE
    assert any("regulate: at target" in e for e in events)
    assert len(tun.calls) == 1                      # converged first shot


def test_regulate_non_convergence_warns_and_records_by_default(tmp_path):
    tun = RecordingSwtTunnel(sim=False, rpm_max=5000.0)
    daq = FixedMachDaq(mach=0.137, sim=False)       # never reaches 0.3
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, daq=daq,
                         tunnel_control_mode="regulate",
                         mach_max_iterations=3)
    events = []
    engine = SweepEngine(mgr, rec, cfg,
                         SweepCallbacks(on_event=events.append))
    out = engine.run([_mach_point(0.3)])[0]
    assert out.status == DONE                        # NON-fatal by default
    assert any("did not converge" in e and "WARNING" in e for e in events)
    assert len(tun.calls) == 3                       # tried max_iterations
    assert Path(out.path).exists()                   # recorded anyway


def test_regulate_faults_when_opted_in(tmp_path):
    tun = RecordingSwtTunnel(sim=False, rpm_max=5000.0)
    daq = FixedMachDaq(mach=0.137, sim=False)
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, daq=daq,
                         tunnel_control_mode="regulate",
                         tunnel_regulate_fault=True, mach_max_iterations=3)
    out = engine_run_one(mgr, rec, cfg, _mach_point(0.3))
    assert out.status == FAILED
    assert "FAULT" in out.error


def engine_run_one(mgr, rec, cfg, point):
    return SweepEngine(mgr, rec, cfg).run([point])[0]


# ═══ AIR-OFF short-circuit — Casey's exact repro ═════════════════════════
def test_casey_hz0_airoff_regulate_records_not_faults(tmp_path):
    """LSWT drive + a tunnel-condition stream pinned at a garbage Mach
    0.137, speed_unit=hz, a 0-Hz air-off point, mode=regulate → the point
    RECORDS (does NOT fault); the fan is commanded to 0 and the
    measured-feedback loop toward 0 NEVER runs."""
    tun = RecordingLswtTunnel(sim=False, max_hz=60.0)
    daq = FixedMachDaq(mach=0.137, sim=False)
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, daq=daq,
                         tunnel_control_mode="regulate", speed_unit="hz",
                         tunnel_regulate_fault=False)
    events = []
    engine = SweepEngine(mgr, rec, cfg,
                         SweepCallbacks(on_event=events.append))
    pt = SweepPoint(alpha=0.0, mach=0.0, dwell_s=0.05, samples=50,
                    meta={"speed_value": 0.0, "speed_unit": "hz"})
    out = engine.run([pt])[0]
    assert out.status == DONE                        # the fault is GONE
    assert tun.calls == [{"hz": pytest.approx(0.0)}]  # fan → 0, once
    assert any("air-off" in e for e in events)
    # the measured-feedback loop NEVER ran toward 0
    assert not any("tunnel regulate" in e for e in events)
    with h5py.File(out.path, "r") as f:
        assert f.attrs["air_state"] == "AirOff"


def test_airoff_swt_commands_rpm_zero(tmp_path):
    tun = RecordingSwtTunnel(sim=False, rpm_max=2000.0)
    daq = FixedMachDaq(mach=0.137, sim=False)
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, daq=daq,
                         tunnel_control_mode="auto")
    out = SweepEngine(mgr, rec, cfg).run(
        [_mach_point(0.0)])[0]
    assert out.status == DONE
    assert tun.calls == [{"rpm": pytest.approx(0.0)}]


# ═══ fan arm/run before an AUTOMATIC velocity sweep (LSWT ACS530) ════════
@pytest.mark.parametrize("mode", ["auto", "regulate"])
def test_auto_arms_and_runs_fan_before_sweep(tmp_path, mode):
    """auto/regulate + a non-air-off LSWT speed point: the engine arms+runs
    the fan (fan_start) and, once it reports running, proceeds."""
    from freestream import speed
    tun = RecordingLswtFanTunnel(sim=False, max_hz=60.0, arms=True)
    daq = FixedMachDaq(mach=0.137, sim=False)
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, daq=daq,
                         tunnel_control_mode=mode, speed_unit="hz")
    events = []
    engine = SweepEngine(mgr, rec, cfg,
                         SweepCallbacks(on_event=events.append))
    pt = SweepPoint(alpha=0.0, mach=speed.mach_from(30.0, "hz"),
                    dwell_s=0.05, samples=50,
                    meta={"speed_value": 30.0, "speed_unit": "hz"})
    out = engine.run([pt])[0]
    assert out.status == DONE
    assert tun.fan_start_calls == 1                  # armed once, up front
    assert any("fan armed and running" in e for e in events)


def test_auto_fan_never_runs_faults_before_point0(tmp_path):
    """A drive that never reports running → the sweep FAULTs before point 0
    with the clear, actionable message; no speed is ever commanded."""
    from freestream import speed
    tun = RecordingLswtFanTunnel(sim=False, max_hz=60.0, arms=False)
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun,
                         tunnel_control_mode="auto", speed_unit="hz",
                         tunnel_timeout_s=0.3)
    engine = SweepEngine(mgr, rec, cfg)
    pt = SweepPoint(alpha=0.0, mach=speed.mach_from(30.0, "hz"),
                    dwell_s=0.05, samples=50,
                    meta={"speed_value": 30.0, "speed_unit": "hz"})
    outcomes = engine.run([pt])
    assert len(outcomes) == 1
    assert outcomes[0].status == FAILED
    assert outcomes[0].index == 0
    assert "did not start/arm" in outcomes[0].error
    assert tun.fan_start_calls == 1
    assert tun.calls == []                           # never commanded a speed


def test_auto_all_airoff_sweep_needs_no_fan(tmp_path):
    """An all-air-off sweep needs no running fan — not faulted even if the
    drive would never arm."""
    tun = RecordingLswtFanTunnel(sim=False, max_hz=60.0, arms=False)
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun,
                         tunnel_control_mode="auto", speed_unit="hz",
                         tunnel_timeout_s=0.3)
    pt = SweepPoint(alpha=0.0, mach=0.0, dwell_s=0.05, samples=50,
                    meta={"speed_value": 0.0, "speed_unit": "hz"})
    out = SweepEngine(mgr, rec, cfg).run([pt])[0]
    assert out.status == DONE
    assert tun.fan_start_calls == 0                  # no fan required


def test_manual_never_arms_fan(tmp_path):
    """Manual is monitor-only: the fan is never written even on a capable
    LSWT drive with a nonzero speed point."""
    from freestream import speed
    tun = RecordingLswtFanTunnel(sim=False, max_hz=60.0, arms=True)
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, speed_unit="hz")  # manual
    assert cfg.tunnel_control_mode == "manual"
    engine = SweepEngine(mgr, rec, cfg, SweepCallbacks(
        on_operator_wait=lambda r: "proceed"))
    pt = SweepPoint(alpha=0.0, mach=speed.mach_from(30.0, "hz"),
                    dwell_s=0.05, samples=50,
                    meta={"speed_value": 30.0, "speed_unit": "hz"})
    out = engine.run([pt])[0]
    assert out.status == DONE
    assert tun.fan_start_calls == 0
    assert tun.calls == []                           # manual never writes


def test_auto_swt_no_fan_start_is_clean_noop(tmp_path):
    """An SWT-style drive has no fan_start (operator/console-run fan) — the
    prepare step is a clean no-op and the sweep runs exactly as before."""
    tun = RecordingSwtTunnel(sim=False, rpm_max=2000.0)
    daq = FixedMachDaq(mach=0.3, sim=False)
    assert not hasattr(tun, "fan_start")
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, daq=daq,
                         tunnel_control_mode="auto")
    out = SweepEngine(mgr, rec, cfg).run([_mach_point(0.3)])[0]
    assert out.status == DONE
    assert tun.calls == [{"rpm": pytest.approx(0.3 * cfg.rpm_per_mach)}]


# ═══ fan STOP after an automatic run (LSWT ACS530 shutdown) ══════════════
def _hz_point(hz, **meta):
    from freestream import speed
    m = 0.0 if hz == 0 else speed.mach_from(hz, "hz")
    meta.setdefault("speed_value", float(hz))
    meta.setdefault("speed_unit", "hz")
    return SweepPoint(alpha=0.0, mach=m, dwell_s=0.05, samples=50, meta=meta)


@pytest.mark.parametrize("mode", ["auto", "regulate"])
def test_auto_run_end_stops_fan_once(tmp_path, mode):
    """auto/regulate normal run end → fan_stop() called exactly once, with
    the 'run complete' log line."""
    tun = RecordingLswtFanTunnel(sim=False, max_hz=60.0, arms=True)
    daq = FixedMachDaq(mach=0.137, sim=False)
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, daq=daq,
                         tunnel_control_mode=mode, speed_unit="hz")
    events = []
    engine = SweepEngine(mgr, rec, cfg,
                         SweepCallbacks(on_event=events.append))
    out = engine.run([_hz_point(30.0)])[0]
    assert out.status == DONE
    assert tun.fan_stop_calls == 1
    assert any("fan stopped (run complete)" in e for e in events)


def test_abort_still_stops_fan_once(tmp_path):
    """A graceful abort() mid-run still fires the run-end fan_stop once."""
    tun = RecordingLswtFanTunnel(sim=False, max_hz=60.0, arms=True)
    daq = FixedMachDaq(mach=0.137, sim=False)
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, daq=daq,
                         tunnel_control_mode="auto", speed_unit="hz")
    engine = SweepEngine(mgr, rec, cfg)

    # abort at the first point boundary → the loop skips + finish; the
    # finally-hook still shuts the fan down.
    engine.abort()
    engine.run([_hz_point(30.0), _hz_point(20.0)])
    assert tun.fan_stop_calls == 1


def test_manual_never_stops_fan(tmp_path):
    """Manual is monitor-only: the fan is never stopped by Freestream."""
    tun = RecordingLswtFanTunnel(sim=False, max_hz=60.0, arms=True)
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, speed_unit="hz")  # manual
    assert cfg.tunnel_control_mode == "manual"
    engine = SweepEngine(mgr, rec, cfg, SweepCallbacks(
        on_operator_wait=lambda r: "proceed"))
    out = engine.run([_hz_point(30.0)])[0]
    assert out.status == DONE
    assert tun.fan_stop_calls == 0


def test_auto_swt_no_fan_stop_is_clean_noop(tmp_path):
    """An SWT-style drive has no fan_stop (operator/console-run fan) — the
    shutdown hook is a clean no-op and the run completes normally."""
    tun = RecordingSwtTunnel(sim=False, rpm_max=2000.0)
    daq = FixedMachDaq(mach=0.3, sim=False)
    assert not hasattr(tun, "fan_stop")
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, daq=daq,
                         tunnel_control_mode="auto")
    out = SweepEngine(mgr, rec, cfg).run([_mach_point(0.3)])[0]
    assert out.status == DONE                         # no crash on no-op


# ═══ metadata: the run swept multiple velocities (Feature 2) ═════════════
def test_hz_sweep_records_speed_meta_and_setpoints(tmp_path):
    """A recorded Hz-sweep file's root attrs carry speed_unit='hz', this
    point's speed_value, AND the run's full speed_setpoints list — a single
    file read can tell both the point's speed and that the run was
    multi-velocity."""
    tun = RecordingLswtFanTunnel(sim=False, max_hz=60.0, arms=True)
    daq = FixedMachDaq(mach=0.137, sim=False)
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, daq=daq,
                         tunnel_control_mode="auto", speed_unit="hz")
    engine = SweepEngine(mgr, rec, cfg)
    pts = [_hz_point(0.0), _hz_point(10.0), _hz_point(30.0)]
    outs = engine.run(pts)
    assert all(o.status == DONE for o in outs)
    with h5py.File(outs[2].path, "r") as f:
        assert f.attrs["speed_unit"] == "hz"
        assert float(f.attrs["speed_value"]) == pytest.approx(30.0)
        setpoints = [float(v) for v in f.attrs["speed_setpoints"]]
        assert setpoints == [0.0, 10.0, 30.0]


def test_mach_sweep_has_no_spurious_speed_keys(tmp_path):
    """A plain mach sweep still carries mach and writes NO speed_setpoints /
    speed_value (nothing spurious)."""
    tun = RecordingSwtTunnel(sim=False, rpm_max=2000.0)
    daq = FixedMachDaq(mach=0.3, sim=False)
    mgr, rec, cfg = _rig(tmp_path, tunnel=tun, daq=daq,
                         tunnel_control_mode="auto")   # speed_unit=mach
    out = SweepEngine(mgr, rec, cfg).run([_mach_point(0.3)])[0]
    with h5py.File(out.path, "r") as f:
        assert "speed_setpoints" not in f.attrs
        assert "speed_value" not in f.attrs
        assert f["Tunnel/Mach_cmd"][0] == pytest.approx(0.3)


# ═══ setup dialog: the 3-way selector ════════════════════════════════════
import os                                            # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QComboBox  # noqa: E402

from freestream.app.setup_dialog import MeasurementSetupDialog  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


def test_dialog_builds_three_way_selector(app):
    dlg = MeasurementSetupDialog(FreestreamConfig())
    combo = dlg.control_mode_combo
    assert isinstance(combo, QComboBox)
    values = [combo.itemData(i) for i in range(combo.count())]
    assert values == ["manual", "auto", "regulate"]
    assert combo.currentData() == "manual"          # default
    # the two old ambiguous checkboxes are gone
    assert not hasattr(dlg, "tunnel_ctl_chk")
    # verify-toggle only meaningful in Manual
    assert dlg.mach_check_chk.isEnabled()
    combo.setCurrentIndex(combo.findData("auto"))
    assert not dlg.mach_check_chk.isEnabled()


def test_dialog_apply_writes_mode_and_legacy_boolean(app):
    cfg = FreestreamConfig()
    dlg = MeasurementSetupDialog(cfg)
    dlg.control_mode_combo.setCurrentIndex(
        dlg.control_mode_combo.findData("regulate"))
    dlg.apply_to(cfg)
    assert cfg.tunnel_control_mode == "regulate"
    assert cfg.tunnel_control_enabled is True        # legacy kept in sync
    # round-trips back into a fresh dialog
    dlg2 = MeasurementSetupDialog(cfg)
    assert dlg2.control_mode_combo.currentData() == "regulate"
    # back to manual clears the legacy boolean
    dlg2.control_mode_combo.setCurrentIndex(
        dlg2.control_mode_combo.findData("manual"))
    dlg2.apply_to(cfg)
    assert cfg.tunnel_control_mode == "manual"
    assert cfg.tunnel_control_enabled is False
