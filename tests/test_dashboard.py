"""Tunnel dashboard tests — the merged Tunnel tab.

Builds the dashboard against the REAL sim TunnelAdapter (tunnel_plc
SimGateway underneath) plus the fake DaqBook, and checks: gauge/lamps
come from the live snapshot, Mach/q tiles come from derived.tunnel_state,
bearing tiles show "—" while the driver feature is disabled and real
values once a sim monitor with ``bearing_temps=True`` is injected.
Offscreen.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication            # noqa: E402

from freestream._fakes import FakeDaq               # noqa: E402
from freestream.adapters.tunnel import TunnelAdapter  # noqa: E402
from freestream.app.tunnel_dashboard import TunnelDashboard  # noqa: E402
from freestream.derived import tunnel_state         # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


class _StubManager:
    """Just enough manager surface for the dashboard."""

    def __init__(self, tunnel=None, daq=None, positioner=None):
        self._tunnel = tunnel
        self.streaming = [daq] if daq is not None else []
        self.setpoint = tunnel
        self.positioner = positioner

    def by_role(self, role):
        if role == "tunnel":
            return self._tunnel
        if role == "positioner":
            return self.positioner
        return None


def _wait(cond, timeout=10.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


@pytest.fixture()
def tunnel():
    a = TunnelAdapter(sim=True)
    yield a
    a.disconnect()


def test_dashboard_builds_with_sim_adapter(app, tunnel):
    daq = FakeDaq()
    daq.connect()
    dash = TunnelDashboard(_StubManager(tunnel, daq))
    try:
        tunnel.connect()
        assert _wait(lambda: tunnel.connected, 5.0)
        dash.active = True
        dash._sample()
        # Mach/q tiles agree with the ONE derived source
        v = daq.latest()
        st = tunnel_state(v["Pdiff"], v["Ptot"], v["Temp"])
        assert dash.tiles["mach"].value.text() == f"{st.mach:.3f}"
        assert dash.tiles["q"].value.text() == f"{st.q_psi:.3f}"
        assert dash.tiles["ptot"].value.text() == f"{v['Ptot']:.2f}"
        # gauge + status lights fed from the live snapshot
        assert dash.gauge is not None and dash.rotor is not None
        assert "bearing_temp_low" in dash.lamps      # prominent warning LED
        assert "console_control" in dash.lamps
        # strip charts exist and got history
        assert dash._hist["mach"] and dash._hist["rpm"]
    finally:
        dash.shutdown()


def test_bearing_status_led_present_tiles_removed(app, tunnel):
    """Analog bearing TILES are gone (channels not in the gateway yet), but
    the working 'Bearing temp low' status LED stays in the VersaMax grid."""
    dash = TunnelDashboard(_StubManager(tunnel))
    try:
        tunnel.connect()
        assert _wait(lambda: tunnel.connected, 5.0)
        dash.active = True
        dash._sample()
        # the three analog tiles were removed from the top band
        for key in ("bearing_b1", "bearing_b2", "bearing_b3"):
            assert key not in dash.tiles
        # the real boolean LED is retained
        assert "bearing_temp_low" in dash.lamps
    finally:
        dash.shutdown()


def test_attitude_indicator_reads_positioner(app, tunnel):
    """The α/β pad sources positions() from the manager's positioner and
    works for both mode1 (crescent) and mode2 (ate) — positions() is the
    same contract for both."""
    from freestream._fakes import FakePositioner
    pos = FakePositioner()
    pos.connect()
    pos.move_to(alpha=3.5, beta=-2.0)
    assert _wait(lambda: pos.settled(), 2.0)
    dash = TunnelDashboard(_StubManager(tunnel, positioner=pos))
    try:
        assert dash._positioner is pos
        dash.set_targets(4.0, -2.0)
        dash.active = True
        dash._sample()
        assert dash._att._alpha == pytest.approx(3.5)
        assert dash._att._beta == pytest.approx(-2.0)
        assert dash._att._alpha_t == pytest.approx(4.0)   # ghost target
        assert dash._att._beta_t == pytest.approx(-2.0)
        # formatting uses a true minus sign and degree mark
        assert dash._att._fmt(-2.0) == "−2.0°"
        assert dash._att._fmt(None) == "—"
    finally:
        dash.shutdown()


def test_attitude_pad_is_prominent_and_band_adapts(app, tunnel):
    """The α/β pad is a PRIMARY instrument: large minimum size, and the
    adaptive top band drops the lamps first, then the fan gauge, keeping
    the pad down to narrow widths."""
    from PyQt6.QtWidgets import QApplication as _QA
    dash = TunnelDashboard(_StubManager(tunnel))
    try:
        dash.show()               # resize events only reach shown widgets
        _QA.processEvents()
        # substantially larger than the old 118x150 compact pad
        assert dash._att.minimumWidth() >= 180
        assert dash._att.minimumHeight() >= 200
        # wide: everything visible
        dash.resize(1600, 950)
        _QA.processEvents()
        assert not dash._lamp_box.isHidden()
        assert not dash._fan_box.isHidden()
        assert not dash._att_box.isHidden()
        # default-window width: lamps drop first, pad + gauge stay
        dash.resize(1000, 700)
        _QA.processEvents()
        assert dash._lamp_box.isHidden()
        assert not dash._fan_box.isHidden()
        assert not dash._att_box.isHidden()
        # narrow: gauge drops, the pad survives (primary instrument)
        dash.resize(840, 700)
        _QA.processEvents()
        assert dash._fan_box.isHidden()
        assert not dash._att_box.isHidden()
        # very narrow: only the stat tiles remain
        dash.resize(560, 700)
        _QA.processEvents()
        assert dash._att_box.isHidden()
    finally:
        dash.close()
        dash.shutdown()


def test_dashboard_survives_fakes_without_snapshot(app):
    """FakeTunnel (no .snapshot) must not crash the dashboard."""
    from freestream._fakes import FakeTunnel
    ft = FakeTunnel()
    ft.connect()
    dash = TunnelDashboard(_StubManager(ft, FakeDaq()))
    try:
        assert dash._tunnel is None                 # not snapshot-capable
        dash.active = True
        dash._sample()                              # no exception
        assert dash.tiles["rpm"].value.text() != ""
    finally:
        dash.shutdown()
