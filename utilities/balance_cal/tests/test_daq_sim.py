"""BalanceDaq against the NI driver's simulator (no hardware)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "devices"))

from balcal_gui.daq import BalanceDaq
from balcal_gui.session import BalanceKind


@pytest.fixture
def daq():
    d = BalanceDaq("ni6351", sim=True, scan_hz=500.0)
    yield d
    d.disconnect()


def test_connect_exposes_balance_channels(daq):
    daq.connect(BalanceKind.FORCE)
    assert daq.connected and daq.sim_mode
    names = daq.driver.channel_names()
    for want in ["N1", "N2", "Y1", "Y2", "Axial", "Roll", "Excitation"]:
        assert want in names


def test_acquire_returns_means_for_all_channels(daq):
    daq.connect(BalanceKind.FORCE)
    acq = daq.acquire(0.3, BalanceKind.FORCE)
    assert set(acq.means) == {"N1", "N2", "Y1", "Y2", "Axial", "Roll",
                              "Excitation"}
    n = acq.t.size
    assert n >= 0.25 * 500
    for v in acq.volts.values():
        assert v.size == n
    assert acq.rate_hz > 0


def test_acquire_uses_fresh_samples(daq):
    daq.connect(BalanceKind.FORCE)
    before = daq.driver.frame_count()
    daq.acquire(0.2, BalanceKind.FORCE)
    assert daq.driver.frame_count() > before


def test_moment_layout_renames_bridges(daq):
    daq.connect(BalanceKind.MOMENT)
    names = daq.driver.channel_names()
    for want in ["AftPitch", "AftYaw", "FwdPitch", "FwdYaw"]:
        assert want in names
    acq = daq.acquire(0.2, BalanceKind.MOMENT)
    assert "AftPitch" in acq.means


def test_tare_on_shared_device_does_not_leak_into_calibration(daq):
    """Freestream may have tared its live device; calibration volts must
    stay absolute. The driver subtracts _tare from the ring's _V fields,
    and BalanceDaq.acquire adds it back."""
    import time
    daq.connect(BalanceKind.FORCE)
    time.sleep(0.3)
    pre = daq.acquire(0.3, BalanceKind.FORCE).means["N1"]
    daq.driver._tare["N1"] = 0.5          # deterministic injected tare
    time.sleep(0.3)                       # let tared samples fill in
    acq = daq.acquire(0.3, BalanceKind.FORCE)
    ring_v = float(daq.driver.ring.tail(50, fields=["N1_V"])
                   ["N1_V"].mean())
    assert ring_v < -0.4                  # ring really is tared
    assert abs(acq.means["N1"] - pre) < 0.05   # cal read unaffected


def test_shared_disconnected_driver_rejected():
    from balcal_gui.daq import BalanceDaq as BD
    inner = BD("ni6351", sim=True)        # never connected
    shared = BD("ni6351", driver=inner.driver)
    with pytest.raises(RuntimeError, match="not connected"):
        shared.connect(BalanceKind.FORCE)


def test_abort_cancels_acquire(daq):
    import threading
    daq.connect(BalanceKind.FORCE)
    threading.Timer(0.2, daq.abort_acquire).start()
    with pytest.raises(RuntimeError, match="cancelled"):
        daq.acquire(30.0, BalanceKind.FORCE)


def test_acquire_requires_connection():
    d = BalanceDaq("ni6351", sim=True)
    with pytest.raises(RuntimeError):
        d.acquire(0.1, BalanceKind.FORCE)


def test_unknown_backend_rejected():
    with pytest.raises(ValueError):
        BalanceDaq("nope", sim=True)
