"""SIM-mode tests for the NiDaqAdapter balance surface + Pdiff channel.

The new North-LSWT mode records the six balance bridges (ai0..ai5),
the supplied excitation (ai6) and the differential-pressure transducer
(ai7, channel name **Pdiff**) on the NI USB-6351. Runs against the
driver's SimCore — no NI-DAQmx, no hardware.
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

from freestream.adapters.ni_daq import GROUP, NiDaqAdapter     # noqa: E402
from freestream.hal import Streaming, Zeroable                 # noqa: E402

BRIDGES = ["N1", "N2", "Y1", "Y2", "Axial", "Roll"]


def test_channel_layout_bridges_excitation_pdiff():
    """balance_cal-compatible bridge layout on ai0..ai5, excitation next,
    Pdiff next — all present-by-default."""
    a = NiDaqAdapter(sim=True)
    specs = a.channels()
    names = [c.name for c in specs]
    assert names == BRIDGES + ["Excitation", "Pdiff"]
    assert all(c.group == GROUP for c in specs)
    # physical AI order is incrementally increasing from ai0
    ai = {c.name: c.channel for c in a.config.enabled_channels()}
    assert [ai[n] for n in BRIDGES] == [0, 1, 2, 3, 4, 5]
    assert ai["Excitation"] == 6
    assert ai["Pdiff"] == 7
    # units: bridges/excitation record raw volts; Pdiff declares its
    # transducer unit from the config
    units = {c.name: c.unit for c in specs}
    assert all(units[n] == "V" for n in BRIDGES)
    assert units["Excitation"] == "V"
    assert units["Pdiff"] == "psid"
    assert isinstance(a, Streaming) and isinstance(a, Zeroable)


def test_balance_file_markers():
    """The recorded file must be self-describing: group marker via
    channels(), balance_type 'internal' (bridge volts needing a .vol),
    vol_path/cal_type device-owned like the strainbook."""
    a = NiDaqAdapter(sim=True)
    assert a.balance_type == "internal"
    assert {c.group for c in a.channels()} == {GROUP}
    assert a.vol_path == a.config.vol_path
    assert a.cal_type == a.config.cal_type == "Linear"


def test_sim_stream_drain_and_raw_tail():
    a = NiDaqAdapter(sim=True)
    a.connect()
    try:
        assert a.connected and a.sim
        assert a.status().ok
        assert a.sample_rate() > 0
        a.start()
        time.sleep(0.5)

        block = a.drain_block()
        for name in BRIDGES + ["Excitation", "Pdiff"]:
            assert name in block, f"missing {name}"
            assert isinstance(block[name], np.ndarray)
            assert block[name].size > 0, f"{name}: empty first drain"

        latest = a.latest()
        for name in BRIDGES + ["Excitation", "Pdiff"]:
            assert isinstance(latest[name], float)
        assert latest["Excitation"] == pytest.approx(10.0, abs=0.1)

        # Forces-monitor tail: non-consuming, excitation kept in
        # engineering volts for balcal normalisation
        tail = a.raw_tail(50)
        assert set(BRIDGES + ["Excitation", "Pdiff"]) <= set(tail)
        assert np.mean(tail["Excitation"]) == pytest.approx(10.0, abs=0.1)
        # tail did not steal the recorder's samples
        time.sleep(0.2)
        block2 = a.drain_block()
        assert block2["N1"].size > 0
    finally:
        a.disconnect()


def test_zero_bumps_zero_count():
    a = NiDaqAdapter(sim=True)
    a.connect()
    try:
        a.start()
        time.sleep(0.4)
        assert a.zero_count == 0
        tares = a.zero(seconds=0.2)
        assert a.zero_count == 1
        assert set(BRIDGES) <= set(tares)      # bridges tared, not Pdiff
        assert "Pdiff" not in tares
        assert "Excitation" not in tares
    finally:
        a.disconnect()


def test_apply_config_dict_preserves_sim_and_reasserts_pdiff():
    """force_sim stays the manager's switch, and a loaded bundle that
    predates the Pdiff/Excitation channels (or disabled them) gets them
    re-asserted so the derived q chain never loses its source."""
    a = NiDaqAdapter(sim=True)
    data = a.config_dict()
    data["force_sim"] = False                  # bundle captured LIVE
    data["scan_hz"] = 500.0
    # simulate an old bundle: no Pdiff, Excitation disabled
    data["channels"] = [c for c in data["channels"]
                        if c["name"] != "Pdiff"]
    for c in data["channels"]:
        if c["name"] == "Excitation":
            c["enabled"] = False
    a.apply_config_dict(data)
    assert a.config.force_sim is True          # session mode preserved
    assert a.config.scan_hz == 500.0           # real settings applied
    names = [c.name for c in a.config.enabled_channels()]
    assert "Excitation" in names and "Pdiff" in names


def test_balance_layout_rename_keeps_extras():
    """Force → Moment renames ONLY the four bridge channels; Axial/Roll/
    Excitation/Pdiff pass through untouched."""
    a = NiDaqAdapter(sim=True)
    a.balance_config = "Moment"
    assert [c.name for c in a.channels()] == \
        ["AftPitch", "AftYaw", "FwdPitch", "FwdYaw",
         "Axial", "Roll", "Excitation", "Pdiff"]
    a.balance_config = "Force"
    assert [c.name for c in a.channels()][:4] == ["N1", "N2", "Y1", "Y2"]
