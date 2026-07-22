"""Balance-layout (Force ↔ Moment) tests for the StrainBook/616.

The four bridge channels (front-panel CH1-4) are physically identical for
both layouts — only their NAMES differ (Streamlined keys reduction on the
names). Force is the default everywhere; switching to Moment RENAMES the
four bridge channels to AftPitch/AftYaw/FwdPitch/FwdYaw and leaves Axial,
Roll and Excitation untouched, on both the config and a LIVE device.
"""

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strainbook_616 import balcal
from strainbook_616.config import (BRIDGE_NAMES, StrainbookConfig,
                                    default_channels)
from strainbook_616.device import Strainbook616

_FORCE = ["N1", "N2", "Y1", "Y2", "Axial", "Roll", "Excitation"]
_MOMENT = ["AftPitch", "AftYaw", "FwdPitch", "FwdYaw", "Axial", "Roll",
           "Excitation"]


def test_default_is_force():
    cfg = StrainbookConfig()
    assert cfg.balance_config == "Force"
    assert [c.name for c in cfg.channels] == _FORCE


def test_default_channels_moment_names():
    chans = default_channels("Moment")
    assert [c.name for c in chans] == _MOMENT
    # bridge positions/ranges unchanged — only the four names moved
    assert [c.channel for c in chans] == [1, 2, 3, 4, 5, 6, 8]
    assert chans[0].range_mv == 11.0 and chans[4].range_mv == 32.0
    assert chans[6].read_excitation


def test_set_balance_config_renames_only_bridges():
    cfg = StrainbookConfig()
    renames = cfg.set_balance_config("Moment")
    assert cfg.balance_config == "Moment"
    assert [c.name for c in cfg.channels] == _MOMENT
    assert renames == {"N1": "AftPitch", "N2": "AftYaw",
                       "Y1": "FwdPitch", "Y2": "FwdYaw"}
    # Axial/Roll/Excitation config objects untouched
    assert cfg.channels[4].name == "Axial"
    assert cfg.channels[6].read_excitation

    # switching back restores the force names (bidirectional map)
    back = cfg.set_balance_config("Force")
    assert [c.name for c in cfg.channels] == _FORCE
    assert back == {"AftPitch": "N1", "AftYaw": "N2",
                    "FwdPitch": "Y1", "FwdYaw": "Y2"}


def test_bridge_names_constant():
    assert BRIDGE_NAMES["Force"] == ("N1", "N2", "Y1", "Y2")
    assert BRIDGE_NAMES["Moment"] == ("AftPitch", "AftYaw",
                                      "FwdPitch", "FwdYaw")


def test_live_device_layout_switch_keeps_streaming():
    """Switching layout on a running sim device renames the ring fields in
    place (history preserved) and the drain/latest keep working."""
    cfg = StrainbookConfig(force_sim=True, scan_hz=500.0)
    dev = Strainbook616(cfg)
    try:
        dev.connect()
        dev.start()
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline and dev.frame_count() < 300:
            time.sleep(0.05)
        assert dev.frame_count() >= 300
        assert "N1" in dev.latest() and "N1_V" in dev.ring.fields

        dev.set_balance_config("Moment")
        assert cfg.balance_config == "Moment"
        assert dev.channel_names()[:4] == ["AftPitch", "AftYaw",
                                           "FwdPitch", "FwdYaw"]
        # ring fields renamed in place, data preserved (no reconnect)
        assert "AftPitch" in dev.ring.fields
        assert "AftPitch_V" in dev.ring.fields
        assert "N1" not in dev.ring.fields
        latest = dev.latest()
        assert "AftPitch" in latest and "N1" not in latest
        # keeps acquiring under the new names
        n0 = dev.frame_count()
        time.sleep(0.2)
        assert dev.frame_count() > n0
        assert "AftPitch" in dev.ring.tail(50)
    finally:
        dev.disconnect()


def test_balcal_reduces_with_moment_config():
    """A moment-named raw block reduces through the same cal as a
    force-named one when balance_config follows."""
    caldir = Path(__file__).resolve().parents[2] / "Streamlined" / "CalFiles"
    vol = caldir / "2025_06_06_2 100 lb.vol"
    if not vol.exists():
        print("  (skipped — Streamlined CalFiles not present)")
        return
    cal = balcal.calc_coeffs(balcal.read_vol_file(str(vol)), "Linear")
    n = 40
    v = np.full(n, 1e-4)
    force_raw = {name: v for name in ("N1", "N2", "Y1", "Y2", "Axial",
                                      "Roll")}
    force_raw["Excitation"] = np.full(n, 10.0)
    moment_raw = {name: v for name in ("AftPitch", "AftYaw", "FwdPitch",
                                       "FwdYaw", "Axial", "Roll")}
    moment_raw["Excitation"] = np.full(n, 10.0)

    brf_f = balcal.calc_brf_forces(force_raw, cal, balance_config="Force")
    brf_m = balcal.calc_brf_forces(moment_raw, cal, balance_config="Moment")
    # both produce finite body-frame loads (the moment path exercises the
    # AftPitch.. names and the moment reduction branch)
    for brf in (brf_f, brf_m):
        for f in ("Fx", "Fy", "Fz", "Mx", "My", "Mz"):
            assert np.all(np.isfinite(getattr(brf, f)))
    assert brf_m.elements.shape == brf_f.elements.shape


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} strainbook balance tests passed.")


if __name__ == "__main__":
    _run_all()
