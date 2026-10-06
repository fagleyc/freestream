"""Recorded Tunnel/Mach_meas and q_meas must come from CALIBRATED channels.

Regression (2026-10-06 F16 check, SWT-AC-Internal): the DaqBook group is
recorded as raw volts with its calibration stored beside each channel, but
the derived Mach_meas/q_meas were computed from the raw means as if they
were psid/psia/degC. A Mach 0.30 point (Pdiff 1.7453 V, Ptot 5.9862 V,
Temp 2.5908 V) was written as Mach_meas 0.72 / q_meas 1.54 psi, while the
calibrated values (and Streamlined's reduction of the same file) give 0.295.
"""

import types

import numpy as np
import pytest

from freestream.sweep import SweepEngine

DAQBOOK_CAL = {
    "Pdiff": {"slope": 0.386949, "offset": 0.0, "unit": "psid",
              "type": "linear"},
    "Ptot": {"slope": 1.92604, "offset": 0.0, "unit": "psia",
             "type": "linear"},
    "Temp": {"slope": 10.0, "offset": 0.0, "unit": "degC", "type": "linear"},
}


def _derive(raw: dict, cal: dict) -> dict:
    stub = types.SimpleNamespace(_tunnel_channel_cal=lambda: cal)
    blocks = {"DaqBook2005": {k: np.full(50, v) for k, v in raw.items()},
              "Tunnel": {"RPM_meas": np.zeros(10)}}
    units = {"Tunnel": {"RPM_meas": "RPM"}}
    SweepEngine._add_derived_tunnel(stub, blocks, units)
    return blocks["Tunnel"]


def test_oct_2026_f16_point_records_the_calibrated_mach():
    tun = _derive({"Pdiff": 1.7453, "Ptot": 5.9862, "Temp": 2.5908},
                  DAQBOOK_CAL)
    assert tun["Mach_meas"][0] == pytest.approx(0.295, abs=0.002)
    assert tun["q_meas"][0] == pytest.approx(0.661, abs=0.005)
    assert len(tun["Mach_meas"]) == 10           # sized like the group


def test_identity_cal_passes_engineering_values_through():
    """Heise-style channels are already engineering units."""
    cal = {k: dict(v, type="identity", slope=99.0) for k, v in
           DAQBOOK_CAL.items()}
    cal["Temp"]["unit"] = "degF"
    tun = _derive({"Pdiff": 0.675, "Ptot": 11.53, "Temp": 78.6}, cal)
    assert tun["Mach_meas"][0] == pytest.approx(0.295, abs=0.003)


def test_uncalibrated_channels_are_used_as_is():
    """Fakes / adapters without tunnel_cal() keep the old behavior."""
    tun = _derive({"Pdiff": 0.675, "Ptot": 11.53, "Temp": 25.9}, {})
    assert tun["Mach_meas"][0] == pytest.approx(0.295, abs=0.003)
