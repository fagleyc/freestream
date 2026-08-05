"""Crescent moves DURING the tunnel ramp (overlapped sequencing).

An opted-in positioner (``overlap_tunnel_ramp`` — the ARC Crescent, in
both SWT and LSWT configurations) is commanded toward the point WHILE
the tunnel is still ramping to its set point; recording still waits for
BOTH position settle and speed settle. Positioners without the opt-in
(sting, traverse, ate) keep the conservative order: tunnel first, then
move. All sim/fakes, no hardware.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freestream._fakes import FakePositioner, FakeTunnel
from freestream.config import FreestreamConfig
from freestream.manager import DeviceManager
from freestream.recorder import Hdf5Recorder
from freestream.runsheet import SweepPoint
from freestream.sweep import DONE, SweepCallbacks, SweepEngine

FAKES = {
    "balance": {"adapter": "freestream._fakes.FakeStreamer", "enabled": True},
    "daq": {"adapter": "freestream._fakes.FakeDaq", "enabled": True},
    "pos": {"adapter": "freestream._fakes.FakePositioner", "enabled": True},
    "tun": {"adapter": "freestream._fakes.FakeTunnel", "enabled": True},
}
MODES = {"mode1": {"positioner": "pos", "balance": "balance",
                   "tunnel_conditions": "daq", "tunnel": "tun"}}


class SlowTunnel(FakeTunnel):
    """FakeTunnel with a LONG ramp; logs command + first settle into a
    shared sequence so tests can assert ordering against the move."""

    RAMP_S = 0.6

    def __init__(self, sim=True):
        super().__init__(sim=sim)
        self.seq = []                    # shared with the positioner
        self._settle_logged = True       # never log a pre-command settle

    def set_target(self, **kw):
        super().set_target(**kw)
        self._at = time.perf_counter() + self.RAMP_S
        self._settle_logged = False
        self.seq.append("set_target")

    def at_target(self):
        ok = super().at_target()
        if ok and not self._settle_logged:
            self._settle_logged = True
            self.seq.append("speed_settled")
        return ok


class RecordingPositioner(FakePositioner):
    """Logs its move command into the tunnel's shared sequence."""

    seq = None                           # wired by the test rig

    def move_to(self, **axes):
        if self.seq is not None:
            self.seq.append("move")
        return super().move_to(**axes)


class OverlapPositioner(RecordingPositioner):
    overlap_tunnel_ramp = True           # the crescent's opt-in


def _rig(tmp_path, positioner, tunnel, **cfg_kw):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"modes": MODES, "devices": FAKES}),
                        encoding="utf-8")
    mgr = DeviceManager("mode1", sim=True, manifest_path=manifest)
    mgr.devices["pos"] = positioner
    mgr.devices["tun"] = tunnel
    positioner.seq = tunnel.seq
    mgr.connect_all()
    for s in mgr.streaming:
        s.start()
    defaults = dict(samples=50, dwell_s=0.05, move_timeout_s=5,
                    tunnel_timeout_s=5, operator="pytest",
                    tunnel_control_mode="auto")
    defaults.update(cfg_kw)
    cfg = FreestreamConfig(**defaults)
    rec = Hdf5Recorder(tmp_path / "runs", config_name="overlaptest")
    return mgr, rec, cfg


def test_crescent_move_commanded_before_speed_settle(tmp_path):
    """The overlap: move_to fires BEFORE the tunnel settle completes (in
    fact before the speed command), and the point still records only
    after both settles (DONE, never early)."""
    pos = OverlapPositioner(settle_s=0.2)
    tun = SlowTunnel()
    mgr, rec, cfg = _rig(tmp_path, pos, tun)
    events = []
    engine = SweepEngine(mgr, rec, cfg,
                         SweepCallbacks(on_event=events.append))
    t0 = time.perf_counter()
    out = engine.run([SweepPoint(alpha=2.0, mach=0.3, dwell_s=0.05,
                                 samples=50)])[0]
    assert out.status == DONE, out.error
    seq = tun.seq
    assert "move" in seq and "speed_settled" in seq, seq
    assert seq.index("move") < seq.index("speed_settled"), seq
    assert seq.index("move") < seq.index("set_target"), seq
    assert any("overlapped with tunnel ramp" in e for e in events)
    # never sampled early: the point took at least the tunnel ramp time
    assert time.perf_counter() - t0 >= SlowTunnel.RAMP_S


def test_non_opted_positioner_keeps_conservative_order(tmp_path):
    """No opt-in → the historical order: speed settles FIRST, then move."""
    pos = RecordingPositioner(settle_s=0.1)   # plain — no overlap attr
    tun = SlowTunnel()
    mgr, rec, cfg = _rig(tmp_path, pos, tun)
    engine = SweepEngine(mgr, rec, cfg)
    out = engine.run([SweepPoint(alpha=2.0, mach=0.3, dwell_s=0.05,
                                 samples=50)])[0]
    assert out.status == DONE, out.error
    seq = tun.seq
    assert seq.index("speed_settled") < seq.index("move"), seq


def test_crescent_adapter_declares_overlap_others_do_not():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "devices"))
    from freestream.adapters.crescent import CrescentAdapter
    from freestream.adapters.lswt_sting import LswtStingAdapter
    from freestream.adapters.traverse import TraverseAdapter
    assert CrescentAdapter.overlap_tunnel_ramp is True
    assert not getattr(LswtStingAdapter, "overlap_tunnel_ramp", False)
    assert not getattr(TraverseAdapter, "overlap_tunnel_ramp", False)
