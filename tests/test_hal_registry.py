"""HAL + DeviceManager registry tests — fakes only, no hardware.

Includes the spec's expandability acceptance test: a new device is ONE
manifest entry; its capabilities and channels appear with no other code.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freestream.hal import (Positioner, SetpointDevice, Streaming, Zeroable,
                           capabilities)
from freestream.manager import DeviceManager


def _manifest(tmp_path, devices, modes) -> Path:
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps({"modes": modes, "devices": devices}),
                 encoding="utf-8")
    return p


FAKES = {
    "balance": {"adapter": "freestream._fakes.FakeStreamer", "enabled": True},
    "daq": {"adapter": "freestream._fakes.FakeDaq", "enabled": True},
    "pos": {"adapter": "freestream._fakes.FakePositioner", "enabled": True},
    "tun": {"adapter": "freestream._fakes.FakeTunnel", "enabled": True},
}
MODES = {"mode1": {"positioner": "pos", "balance": "balance",
                   "tunnel_conditions": "daq", "tunnel": "tun"}}


def test_registry_builds_and_answers_capabilities(tmp_path):
    mgr = DeviceManager("mode1", sim=True,
                        manifest_path=_manifest(tmp_path, FAKES, MODES))
    assert set(mgr.devices) == {"balance", "daq", "pos", "tun"}
    assert isinstance(mgr.positioner, Positioner)
    assert isinstance(mgr.setpoint, SetpointDevice)
    streaming = mgr.streaming
    assert len(streaming) == 2 and all(isinstance(s, Streaming)
                                       for s in streaming)
    assert any(isinstance(d, Zeroable) for d in mgr.devices.values())
    assert "positioner" in capabilities(mgr.positioner)


def test_connect_all_and_blockers(tmp_path):
    mgr = DeviceManager("mode1", sim=True,
                        manifest_path=_manifest(tmp_path, FAKES, MODES))
    blockers = mgr.record_blockers()
    assert blockers, "disconnected devices must block recording"
    assert mgr.connect_all() == {}
    assert mgr.record_blockers() == []
    mgr.devices["daq"].disconnect()
    assert any("daq" in b for b in mgr.record_blockers())
    mgr.disconnect_all()


def test_estop_reaches_every_positioner(tmp_path):
    mgr = DeviceManager("mode1", sim=True,
                        manifest_path=_manifest(tmp_path, FAKES, MODES))
    mgr.connect_all()
    mgr.stop_all_motion()
    assert mgr.devices["pos"].stopped


def test_new_device_is_one_manifest_line(tmp_path):
    """Spec §10: adding a device = registry line, nothing else."""
    devices = dict(FAKES)
    devices["traverse2"] = {"adapter": "freestream._fakes.FakePositioner",
                            "enabled": True}
    modes = {"mode1": dict(MODES["mode1"], traverse="traverse2")}
    mgr = DeviceManager("mode1", sim=True,
                        manifest_path=_manifest(tmp_path, devices, modes))
    assert "traverse2" in mgr.devices
    assert isinstance(mgr.by_role("traverse"), Positioner)
    # its status card data + capabilities come for free
    st = mgr.all_status()["traverse2"]
    assert st.state == "OFFLINE"


def test_unknown_mode_rejected(tmp_path):
    try:
        DeviceManager("modeX", sim=True,
                      manifest_path=_manifest(tmp_path, FAKES, MODES))
        raise AssertionError("unknown mode accepted")
    except ValueError:
        pass
