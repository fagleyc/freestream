"""SIM-mode tests for the HeiseAdapter (tunnel-conditions Ptot/Temp).

Runs against the heise serial emulator (ambient ~14.7 psi pressure,
~72 deg RTD) — no hardware, no Qt.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np
import pytest

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "devices"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from freestream.adapters.heise import GROUP, HeiseAdapter      # noqa: E402
from freestream.hal import Streaming, Zeroable, capabilities   # noqa: E402


def _fast(a: HeiseAdapter) -> HeiseAdapter:
    a.config.poll_s = 0.05                     # ~20 Hz poll for tests
    return a


def test_identity_channels_and_capabilities():
    a = HeiseAdapter(sim=True)
    assert a.id == "heise"
    specs = a.channels()
    # the derived Mach/q chain keys on these EXACT names
    assert [c.name for c in specs] == ["Ptot", "Temp"]
    assert all(c.group == GROUP for c in specs)
    assert all(c.kind == "tunnel" for c in specs)
    units = {c.name: c.unit for c in specs}
    assert units["Ptot"] == "psi"              # absolute sensor → psia
    assert units["Temp"] == "C"
    assert isinstance(a, Streaming)
    # must NOT look like a balance (custom-mode role derivation prefers
    # Zeroables as balance; the Heise's instrument ZERO is not exposed)
    assert not isinstance(a, Zeroable)
    assert "streaming" in capabilities(a)


def test_sim_connect_latest_and_drain():
    a = _fast(HeiseAdapter(sim=True))
    a.connect()
    try:
        assert a.connected and a.sim
        assert a.status().ok
        assert a.sample_rate() == pytest.approx(20.0)
        a.start()
        time.sleep(0.6)

        latest = a.latest()
        assert set(latest) == {"Ptot", "Temp"}
        # emulator physics: ambient absolute pressure, room-ish RTD
        assert latest["Ptot"] == pytest.approx(14.7, abs=0.5)
        assert latest["Temp"] == pytest.approx(72.4, abs=2.0)

        block = a.drain_block()
        assert set(block) == {"Ptot", "Temp"}
        for name, arr in block.items():
            assert isinstance(arr, np.ndarray)
            assert arr.size > 0, f"{name}: empty first drain"

        # cursor semantics: immediate second drain is (near) empty,
        # a later drain picks up only the new samples
        n1 = block["Ptot"].size
        assert a.drain_block()["Ptot"].size <= 2
        time.sleep(0.3)
        block3 = a.drain_block()
        assert 0 < block3["Ptot"].size < n1 + 20

        st = a.status()
        assert st.ok and st.last_sample_age_s is not None
        assert st.last_sample_age_s < 5.0
        a.stop()
    finally:
        a.disconnect()


def test_raw_tail_does_not_move_cursor():
    a = _fast(HeiseAdapter(sim=True))
    a.connect()
    try:
        a.start()
        time.sleep(0.4)
        tail = a.raw_tail(3)
        assert set(tail) == {"Ptot", "Temp"}
        assert tail["Ptot"].size > 0
        # the recorder's drain still sees everything
        assert a.drain_block()["Ptot"].size >= tail["Ptot"].size
    finally:
        a.disconnect()


def test_no_set_sample_rate():
    """The serial indicator cannot follow the suite DAQ rate — honesty
    rule: no set_sample_rate (like the ATE's fixed frame rate)."""
    a = HeiseAdapter(sim=True)
    assert not hasattr(a, "set_sample_rate")
    assert a.sample_rate() > 0


def test_apply_config_dict_preserves_sim_and_channel_names():
    """force_sim is the manager's switch; the canonical Ptot/Temp names
    survive any loaded bundle (the derived chain keys on them)."""
    a = HeiseAdapter(sim=True)
    data = a.config_dict()
    data["force_sim"] = False                  # bundle captured LIVE
    data["left"]["name"] = "Pressure"          # stock driver names
    data["right"]["name"] = "Temperature"
    data["poll_s"] = 0.5
    a.apply_config_dict(data)
    assert a.config.force_sim is True          # session mode preserved
    assert a.config.poll_s == 0.5              # real settings applied
    assert [c.name for c in a.channels()] == ["Ptot", "Temp"]
