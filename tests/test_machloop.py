"""MachLoop tests — Mach→RPM conversion, sim proxy, live isentropic
closure with corrections, clamping, and the bounded-iteration FAULT."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Dict

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freestream._fakes import FakeDaq, FakeTunnel
from freestream.config import FreestreamConfig
from freestream.derived import GAMMA, tunnel_state
from freestream.hal import ChannelSpec
from freestream.machloop import MachLoop, find_tunnel_daq

PTOT_PSIA = 11.38          # SSWT-plausible total pressure
TEMP_C = 21.0


def pdiff_for_mach(mach: float, ptot: float = PTOT_PSIA) -> float:
    """Invert the isentropic chain: the Pdiff [psi] that reads as *mach*."""
    ratio = (1.0 + 0.5 * (GAMMA - 1.0) * mach ** 2) ** (GAMMA / (GAMMA - 1.0))
    return ptot - ptot / ratio


def _wait(cond, timeout_s, msg):
    """Minimal engine-style wait: poll until true or raise TimeoutError."""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        if cond():
            return
        time.sleep(0.01)
    raise TimeoutError(msg)


class LiveTunnel(FakeTunnel):
    """FakeTunnel flavored as hardware, with an adapter-style rpm_max."""

    def __init__(self, rpm_max: float = 1000.0):
        super().__init__(sim=False)
        self.config = SimpleNamespace(rpm_max=rpm_max)

    @property
    def rpm(self) -> float:
        self.at_target()               # settle the fake plant
        return self._rpm


class LiveMachDaq:
    """Streaming stand-in serving pressures CONSISTENT with the tunnel's
    actual RPM: Mach_true = rpm / rpm_per_mach_true, inverted through the
    same isentropic relations the loop uses to measure."""

    def __init__(self, tunnel: LiveTunnel, rpm_per_mach_true: float):
        self.id = "live_mach_daq"
        self.label = "Live Mach Daq"
        self.sim = False
        self._tunnel = tunnel
        self._k = rpm_per_mach_true

    def channels(self):
        return [ChannelSpec(name=c, unit="V", group="DaqBook2005",
                            kind="tunnel", device_id=self.id)
                for c in ("Pdiff", "Ptot", "Temp")]

    def latest(self) -> Dict[str, float]:
        mach = self._tunnel.rpm / self._k
        return {"Pdiff": pdiff_for_mach(mach), "Ptot": PTOT_PSIA,
                "Temp": TEMP_C}


# ── helpers under test ───────────────────────────────────────────────────
def test_pdiff_inversion_round_trips_through_isentropic_chain():
    for mach in (0.1, 0.3, 0.55, 0.8):
        st = tunnel_state(pdiff_for_mach(mach), PTOT_PSIA, TEMP_C)
        assert st.valid
        assert st.mach == pytest.approx(mach, abs=1e-9)


def test_find_tunnel_daq_matches_group():
    daq, other = FakeDaq(), FakeTunnel()
    assert find_tunnel_daq([other, daq]) is daq
    assert find_tunnel_daq([other]) is None


def test_rpm_for_conversion_and_clamp():
    cfg = FreestreamConfig(rpm_per_mach=1500.0)
    loop = MachLoop(LiveTunnel(rpm_max=1000.0), cfg)
    assert loop.rpm_for(0.3) == pytest.approx(450.0)
    assert loop.rpm_for(0.9) == 1000.0            # 1350 clamped to rpm_max
    assert loop.rpm_for(-0.1) == 0.0              # never negative
    # no adapter limit (FakeTunnel has no config) → unclamped
    assert MachLoop(FakeTunnel(), cfg).rpm_for(0.9) == pytest.approx(1350.0)


# ── SIM: proxied by RPM ──────────────────────────────────────────────────
def test_sim_path_proxies_by_rpm_with_log_line():
    cfg = FreestreamConfig(rpm_per_mach=1500.0, tunnel_timeout_s=5.0)
    events = []
    tunnel, daq = FakeTunnel(sim=True), FakeDaq(sim=True)
    loop = MachLoop(tunnel, cfg, daq=daq, event=events.append)
    assert loop.live is False
    res = loop.run(0.3, _wait)
    assert res.proxied is True
    assert res.rpm_cmd == pytest.approx(450.0)
    assert tunnel.readback()["rpm_set"] == pytest.approx(450.0)
    assert any("sim: Mach loop proxied by RPM" in e for e in events)


# ── LIVE: isentropic closure ─────────────────────────────────────────────
def test_live_at_target_first_command_when_map_is_right():
    # plant truth equals the configured map → first command lands inside tol
    cfg = FreestreamConfig(rpm_per_mach=1500.0, mach_tolerance=0.01,
                           tunnel_timeout_s=5.0)
    tunnel = LiveTunnel(rpm_max=2000.0)
    loop = MachLoop(tunnel, cfg, daq=LiveMachDaq(tunnel, 1500.0))
    assert loop.live is True
    res = loop.run(0.3, _wait)
    assert res.proxied is False
    assert res.iterations == 1
    assert res.rpm_cmd == pytest.approx(450.0)
    # at_target was decided from the MEASURED isentropic Mach
    assert res.mach_meas == pytest.approx(0.3, abs=cfg.mach_tolerance)


def test_live_proportional_correction_converges():
    # plant truth 1200 RPM/Mach but config says 1500: first command 450 RPM
    # reads Mach 0.375 → corrected to 450*(0.3/0.375)=360 → Mach 0.300
    cfg = FreestreamConfig(rpm_per_mach=1500.0, mach_tolerance=0.01,
                           mach_max_iterations=3, tunnel_timeout_s=5.0)
    tunnel = LiveTunnel(rpm_max=2000.0)
    loop = MachLoop(tunnel, cfg, daq=LiveMachDaq(tunnel, 1200.0))
    res = loop.run(0.3, _wait)
    assert res.iterations == 2
    assert res.rpm_cmd == pytest.approx(360.0, abs=1.0)
    assert res.mach_meas == pytest.approx(0.3, abs=cfg.mach_tolerance)


def test_live_faults_after_max_iterations_no_runaway():
    class StuckDaq(LiveMachDaq):
        """Pressures pinned at Mach 0.2 no matter what the fan does."""
        def latest(self):
            return {"Pdiff": pdiff_for_mach(0.2), "Ptot": PTOT_PSIA,
                    "Temp": TEMP_C}

    cfg = FreestreamConfig(rpm_per_mach=1500.0, mach_tolerance=0.01,
                           mach_max_iterations=3, tunnel_timeout_s=5.0)
    tunnel = LiveTunnel(rpm_max=2000.0)
    loop = MachLoop(tunnel, cfg, daq=StuckDaq(tunnel, 1500.0))
    with pytest.raises(RuntimeError, match="FAULT"):
        loop.run(0.5, _wait)
    # exactly max_iterations commands, every one clamped inside the limit
    assert 0.0 <= tunnel.readback()["rpm_set"] <= 2000.0


def test_live_correction_clamped_to_rpm_max():
    # plant reads far too slow → correction wants a huge RPM; must clamp
    class SlowDaq(LiveMachDaq):
        def latest(self):
            return {"Pdiff": pdiff_for_mach(0.05), "Ptot": PTOT_PSIA,
                    "Temp": TEMP_C}

    cfg = FreestreamConfig(rpm_per_mach=1500.0, mach_max_iterations=2,
                           tunnel_timeout_s=5.0)
    tunnel = LiveTunnel(rpm_max=800.0)
    loop = MachLoop(tunnel, cfg, daq=SlowDaq(tunnel, 1500.0))
    with pytest.raises(RuntimeError, match="FAULT"):
        loop.run(0.5, _wait)
    assert tunnel.readback()["rpm_set"] == pytest.approx(800.0)  # clamped
