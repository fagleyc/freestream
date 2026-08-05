"""E-stop commands EVERY device, not just the positioners.

Rig-found: the panel E-STOP stopped motion but left the LSWT tunnel fan
running. ``DeviceManager.estop_all`` (the engine's/GUI's E-stop path)
must stop all Positioners AND drive every tunnel SetpointDevice to
stop/zero speed — adapter-native ``estop()``/``fan_stop()`` when
available, else ``set_target(rpm=0)``. All in sim, no hardware.
"""

import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "devices"))

from freestream.config import FreestreamConfig
from freestream.manager import DeviceManager
from freestream.recorder import Hdf5Recorder
from freestream.runsheet import build_grid
from freestream.sweep import SweepEngine

FAKES = {
    "balance": {"adapter": "freestream._fakes.FakeStreamer", "enabled": True},
    "daq": {"adapter": "freestream._fakes.FakeDaq", "enabled": True},
    "pos": {"adapter": "freestream._fakes.FakePositioner", "enabled": True},
    "tun": {"adapter": "freestream._fakes.FakeTunnel", "enabled": True},
}
MODES = {"mode1": {"positioner": "pos", "balance": "balance",
                   "tunnel_conditions": "daq", "tunnel": "tun"}}


def _rig(tmp_path, **cfg_kw):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"modes": MODES, "devices": FAKES}),
                        encoding="utf-8")
    mgr = DeviceManager("mode1", sim=True, manifest_path=manifest)
    mgr.connect_all()
    for s in mgr.streaming:
        s.start()
    defaults = dict(samples=200, dwell_s=0.1, move_timeout_s=5,
                    tunnel_timeout_s=5, operator="pytest")
    defaults.update(cfg_kw)
    cfg = FreestreamConfig(**defaults)
    rec = Hdf5Recorder(tmp_path / "runs", config_name="estoptest")
    return mgr, rec, cfg


def _wait(cond, timeout=10.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_estop_stops_lswt_tunnel_adapter(tmp_path):
    """E-stop mid-sweep reaches the REAL LSWT tunnel adapter: the drive
    gets an immediate STOP word + zero reference (the rig failure)."""
    from freestream.adapters.lswt import LswtTunnelAdapter
    mgr, rec, cfg = _rig(tmp_path)
    lswt = LswtTunnelAdapter(sim=True)
    lswt.id = "tun"
    mgr.devices["tun"] = lswt                # real adapter in the tunnel slot
    lswt.connect()
    try:
        lswt.set_target(hz=30.0)             # sim auto-starts the fan
        assert _wait(lambda: lswt.snapshot().fan_running, 5.0)
        mgr.devices["pos"]._settle_s = 5.0   # E-stop lands mid-move
        engine = SweepEngine(mgr, rec, cfg)
        points = build_grid(alpha_spec="0:1:8", dwell_s=0.1, samples=100)
        t = threading.Thread(target=engine.run, args=(points,), daemon=True)
        t.start()
        time.sleep(0.4)
        engine.estop()
        t.join(timeout=5)
        assert not t.is_alive()
        snap = lswt.snapshot()               # STOP word + zero reference
        assert not snap.fan_running
        assert snap.setpoint_hz == 0.0
        assert mgr.devices["pos"].stopped    # motion stopped too
        assert not engine.running
    finally:
        lswt.disconnect()


def test_estop_stops_swt_tunnel_adapter_sim():
    """SWT PLC path: estop presses the fan STOP button + zeroes RPM_Set —
    even though the guarded write path was never armed by a sweep."""
    from freestream.adapters.tunnel import TunnelAdapter
    tun = TunnelAdapter(sim=True)
    tun.connect()
    try:
        assert _wait(lambda: tun.connected, 5.0)
        tun.set_target(rpm=400.0)            # sim also starts the fan
        assert _wait(lambda: tun.snapshot().fan_running, 5.0)
        tun.estop()
        assert _wait(lambda: not tun.snapshot().fan_running, 5.0)
        assert _wait(lambda: tun.snapshot().rpm_set == 0.0, 5.0)
    finally:
        tun.disconnect()


def test_estop_all_zeroes_plain_setpoint_device(tmp_path):
    """A tunnel with no estop()/fan_stop() still gets its speed zeroed,
    and the positioners still stop."""
    mgr, _, _ = _rig(tmp_path)
    tun = mgr.devices["tun"]                 # FakeTunnel: set_target only
    tun.set_target(rpm=500.0)
    assert _wait(tun.at_target, 5.0)
    mgr.estop_all()
    assert tun.readback()["rpm_set"] == 0.0
    assert mgr.devices["pos"].stopped
