"""SIM-mode smoke tests for every freestream device adapter.

Builds each adapter directly (as the DeviceManager would:
``cls(sim=True, **options)``), connects, and exercises its declared
capabilities against the drivers' emulators — no hardware, no Qt.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

_PROJECTS = Path(__file__).resolve().parents[2]
for _p in (_PROJECTS / "freestream", _PROJECTS / "freestream" / "devices"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from freestream.adapters.ate import AteBalanceAdapter          # noqa: E402
from freestream.adapters.crescent import CrescentAdapter      # noqa: E402
from freestream.adapters.daqbook import DaqbookAdapter        # noqa: E402
from freestream.adapters.strainbook import StrainbookAdapter  # noqa: E402
from freestream.adapters.traverse import TraverseAdapter      # noqa: E402
from freestream.adapters.tunnel import TunnelAdapter          # noqa: E402


def _wait_settled(adapter, timeout=45.0):
    # 45 s: the sim traverse settles in ~24 s wall-clock (real emulator
    # motion, not a stub) — the old 20 s budget made this test flaky on
    # a loaded machine and consistently red on a slow filesystem.
    deadline = time.time() + timeout
    while time.time() < deadline:
        if adapter.settled():
            return True
        time.sleep(0.05)
    return False


def _connected(adapter):
    """Context helper: connect, yield-ish via try/finally in callers."""
    adapter.connect()
    return adapter


# ── streaming (strainbook / daqbook / ate) ───────────────────────────────
@pytest.mark.parametrize("factory", [
    StrainbookAdapter, DaqbookAdapter, AteBalanceAdapter,
], ids=["strainbook", "daqbook", "ate"])
def test_streaming_drain_and_latest(factory):
    a = factory(sim=True)
    a.connect()
    try:
        assert a.connected and a.sim
        assert a.status().ok
        assert a.sample_rate() > 0
        specs = a.channels()
        assert specs

        a.start()
        time.sleep(0.6)
        block = a.drain_block()
        for spec in specs:
            assert spec.name in block, f"missing {spec.name}"
            arr = block[spec.name]
            assert isinstance(arr, np.ndarray)
            assert arr.size > 0, f"{spec.name}: empty first drain"

        latest = a.latest()
        for spec in specs:
            assert isinstance(latest[spec.name], float)

        # immediate second drain: only samples since the first drain
        first_n = block[specs[0].name].size
        block2 = a.drain_block()
        assert block2[specs[0].name].size < max(first_n, 10)

        # after another dwell the cursor picks up the new samples only
        time.sleep(0.3)
        block3 = a.drain_block()
        n3 = block3[specs[0].name].size
        assert 0 < n3 < first_n + block2[specs[0].name].size + \
            int(a.sample_rate())
        a.stop()
    finally:
        a.disconnect()


def test_unified_sample_rate_support():
    """DAQ front-ends honor set_sample_rate (into the driver's scan_hz);
    the ATE frame stream is fixed-rate and must never pretend otherwise."""
    for factory in (StrainbookAdapter, DaqbookAdapter):
        a = factory(sim=True)
        a.set_sample_rate(150.0)
        assert a.config.scan_hz == 150.0
        assert a.sample_rate() == 150.0   # cfg rate until acquisition runs

    ate = AteBalanceAdapter(sim=True)
    assert not hasattr(ate, "set_sample_rate")
    assert ate.sample_rate() > 0          # honest fixed rate


def test_streaming_status_has_sample_age():
    a = DaqbookAdapter(sim=True)
    a.connect()
    try:
        a.start()
        time.sleep(0.3)
        st = a.status()
        assert st.ok
        assert st.last_sample_age_s is not None
        assert st.last_sample_age_s < 5.0
    finally:
        a.disconnect()


# ── positioners ──────────────────────────────────────────────────────────
def test_crescent_positioner():
    a = CrescentAdapter(sim=True)
    a.connect()
    try:
        assert a.status().ok
        names = {ax.name for ax in a.axes()}
        assert names == {"alpha", "beta"}
        handle = a.move_to(alpha=0.5, beta=-0.5)
        assert handle.targets == {"alpha": 0.5, "beta": -0.5}
        assert _wait_settled(a), "crescent move did not settle in time"
        pos = a.positions()
        assert pos["alpha"] == pytest.approx(0.5, abs=0.2)
        assert pos["beta"] == pytest.approx(-0.5, abs=0.2)
        a.stop_all()
    finally:
        a.disconnect()


def test_traverse_positioner():
    a = TraverseAdapter(sim=True)
    a.connect()
    try:
        assert a.status().ok
        names = {ax.name for ax in a.axes()}
        assert names == {"x", "y", "z"}
        # targets inside the current soft limits (Y/Z ranges start at
        # the −18" homing datum; spans are placeholders — see the
        # traverse README TODO)
        a.move_to(x=0.3, y=-17.5, z=-17.9)
        assert _wait_settled(a), "traverse move did not settle in time"
        pos = a.positions()
        assert pos["x"] == pytest.approx(0.3, abs=0.1)
        assert pos["y"] == pytest.approx(-17.5, abs=0.1)
        assert pos["z"] == pytest.approx(-17.9, abs=0.1)
        a.stop_all()
    finally:
        a.disconnect()


def test_ate_positioner_and_zero():
    a = AteBalanceAdapter(sim=True)
    a.connect()
    try:
        assert a.status().ok
        names = {ax.name for ax in a.axes()}
        assert names == {"alpha", "beta"}
        a.move_to(alpha=5.0, beta=10.0)
        assert not a.settled()            # MOVING until *_COMPLETE
        assert _wait_settled(a), "ate move did not settle in time"
        pos = a.positions()
        assert pos["alpha"] == pytest.approx(5.0, abs=0.05)
        assert pos["beta"] == pytest.approx(10.0, abs=0.05)

        tares = a.zero()
        # truth-naming: the ATE tares under the balance-frame axes
        assert set(tares) == {"Fx", "Fy", "Fz", "Mx", "My", "Mz"}
        for v in tares.values():
            assert isinstance(v, float)
        a.stop_all()
    finally:
        a.disconnect()


def test_ate_truth_naming_and_load_limits():
    """The ATE records the TRUE device: group ATE_Balance, the
    balance-frame axis names (Fx..Mz — the device measures balance-frame
    components, not wind loads), and whatever units the OGI is
    configured to send — no StrainBook aliasing. load_limits comes
    defensively from the driver config's max_loads (missing/0 = no
    limit)."""
    a = AteBalanceAdapter(sim=True)
    load_specs = [c for c in a.channels() if c.group != "Positioner"]
    assert [c.group for c in load_specs] == ["ATE_Balance"] * 6
    assert [c.name for c in load_specs] == \
        ["Fx", "Fy", "Fz", "Mx", "My", "Mz"]
    units = {c.name: c.unit for c in load_specs}
    # the rig's OGI is on Lb / Lbft, so that is the default stamp;
    # the units are NOT hardcoded, they follow AteConfig.load_units
    assert units["Fx"] == units["Fy"] == units["Fz"] == "lbf"
    assert units["Mx"] == units["My"] == units["Mz"] == "lbf*ft"
    a._cfg.load_units = "N"
    si = {c.name: c.unit for c in a.channels() if c.group != "Positioner"}
    assert si["Fz"] == "N" and si["My"] == "N*m"
    a._cfg.load_units = "lb"
    assert a.extra_meta()["balance_type"] == "external"
    assert a.balance_type == "external"

    # defensive load_limits: works whether or not the driver config has
    # grown the max_loads field yet
    limits = a.load_limits
    assert set(limits) == {"Fx", "Fy", "Fz", "Mx", "My", "Mz"}
    if not hasattr(a.config, "max_loads"):
        assert all(v == 0.0 for v in limits.values())
    # a config carrying max_loads (partial / zero / junk) maps through
    # basis == streamed here, so the numbers pass through unconverted
    a._cfg.__dict__["max_load_units"] = "lb"
    a._cfg.__dict__["max_loads"] = {"Fz": 450.0, "Fx": 0,
                                    "My": "junk"}
    try:
        limits = a.load_limits
        assert limits["Fz"] == 450.0
        assert limits["Fx"] == 0.0            # 0 → no limit
        assert limits["My"] == 0.0            # junk → no limit
        assert limits["Mx"] == 0.0            # missing → no limit
    finally:
        a._cfg.__dict__.pop("max_loads", None)
        a._cfg.__dict__.pop("max_load_units", None)


def test_positioner_limit_refused():
    a = CrescentAdapter(sim=True)
    a.connect()
    try:
        with pytest.raises(ValueError):
            a.move_to(alpha=999.0)
    finally:
        a.disconnect()


# ── balance tare (Mode 1) ────────────────────────────────────────────────
def test_strainbook_zero():
    a = StrainbookAdapter(sim=True)
    a.connect()
    try:
        a.start()
        time.sleep(0.6)                   # ring needs data to tare on
        tares = a.zero(seconds=0.2)
        assert "N1" in tares
        assert all(isinstance(v, float) for v in tares.values())
    finally:
        a.disconnect()


# ── balance layout (Force ↔ Moment) — single source of truth ─────────────
def test_strainbook_adapter_reports_force_channels_by_default():
    a = StrainbookAdapter(sim=True)
    assert a.balance_config == "Force"
    names = [c.name for c in a.channels()]
    assert names == ["N1", "N2", "Y1", "Y2", "Axial", "Roll", "Excitation"]


def test_strainbook_adapter_channels_follow_moment_switch():
    """Setting adapter.balance_config renames the four bridge channels so the
    recorder (which reads channels()/drain_block) writes the moment names."""
    a = StrainbookAdapter(sim=True)
    a.connect()
    try:
        a.start()
        time.sleep(0.4)
        a.balance_config = "Moment"
        assert a.balance_config == "Moment"
        names = [c.name for c in a.channels()]
        assert names == ["AftPitch", "AftYaw", "FwdPitch", "FwdYaw",
                         "Axial", "Roll", "Excitation"]
        # a LIVE drain (no reconnect) yields the moment names as keys
        time.sleep(0.3)
        block = a.drain_block()
        for n in ("AftPitch", "AftYaw", "FwdPitch", "FwdYaw"):
            assert n in block and block[n].size > 0
        assert "N1" not in block
        # switching back restores the force names
        a.balance_config = "Force"
        assert [c.name for c in a.channels()][:4] == \
            ["N1", "N2", "Y1", "Y2"]
    finally:
        a.disconnect()


# ── tunnel setpoint ──────────────────────────────────────────────────────
def test_tunnel_setpoint():
    a = TunnelAdapter(sim=True)
    a.connect()
    try:
        assert a.status().ok
        a.set_target(rpm=300.0)
        deadline = time.time() + 10.0     # sim ramps 200 RPM/s
        while time.time() < deadline and not a.at_target():
            time.sleep(0.1)
        assert a.at_target(), "tunnel never reached 300 RPM in sim"
        rb = a.readback()
        assert set(rb) == {"rpm", "rpm_set"}
        assert rb["rpm_set"] == pytest.approx(300.0, abs=1.0)
        assert rb["rpm"] == pytest.approx(300.0, abs=30.0)
    finally:
        a.disconnect()


def test_tunnel_rejects_non_rpm_setpoint():
    a = TunnelAdapter(sim=True)
    a.connect()
    try:
        with pytest.raises(ValueError):
            a.set_target(mach=0.3)
    finally:
        a.disconnect()
