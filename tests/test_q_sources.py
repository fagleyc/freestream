"""Generalized tunnel-condition sourcing (derived.py) — Pdiff/Ptot/Temp
found BY CHANNEL NAME across the registry's streaming devices:

* cross-device split (LSWT shape: Pdiff on one device, Ptot/Temp on
  another) derives the same TunnelState as a single DAQ;
* the single-DAQ fast path stays ONE ``latest()`` call per read;
* any missing channel degrades to None (q = None), exactly as the old
  DaqBook-only path did;
* the source map is cached per registry.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from freestream._fakes import FakeDaq, FakeStreamer
from freestream.derived import (TUNNEL_CONDITION_CHANNELS,
                                live_tunnel_state, read_tunnel_conditions,
                                tunnel_condition_sources, tunnel_state)

PDIFF, PTOT, TEMP = 0.44, 11.38, 21.0


class _Mgr:
    """Just enough manager surface (a .streaming list) for the helpers."""

    def __init__(self, streams):
        self.streaming = list(streams)


class NiLike(FakeStreamer):
    """Balance DAQ that also carries the Pdiff transducer (LSWT NI)."""

    def __init__(self):
        super().__init__(group="NI_USB_6351",
                         channels=("N1", "N2", "Pdiff"))
        self.latest_calls = 0

    def latest(self):
        self.latest_calls += 1
        return {"N1": 0.1, "N2": 0.2, "Pdiff": PDIFF}


class HeiseLike(FakeStreamer):
    """Ptot/Temp indicator (LSWT Heise)."""

    def __init__(self):
        super().__init__(group="Heise", channels=("Ptot", "Temp"))
        self.latest_calls = 0

    def latest(self):
        self.latest_calls += 1
        return {"Ptot": PTOT, "Temp": TEMP}


class CountingDaq(FakeDaq):
    def __init__(self):
        super().__init__()
        self.latest_calls = 0

    def latest(self):
        self.latest_calls += 1
        return super().latest()


def test_sources_found_by_name_across_devices():
    ni, he = NiLike(), HeiseLike()
    mgr = _Mgr([ni, he])
    src = tunnel_condition_sources(mgr)
    assert src == {"Pdiff": ni, "Ptot": he, "Temp": he}
    # cached per registry: the same mapping object comes back
    assert tunnel_condition_sources(mgr) is src


def test_cross_device_state_matches_single_device_chain():
    mgr = _Mgr([NiLike(), HeiseLike()])
    st = live_tunnel_state(mgr)
    ref = tunnel_state(PDIFF, PTOT, TEMP)
    assert st is not None and st.valid
    assert st.mach == ref.mach
    assert st.q_psi == ref.q_psi
    # one latest() per source device, not per channel
    assert all(s.latest_calls == 1 for s in mgr.streaming)


def test_single_daq_fast_path_one_latest_call():
    daq = CountingDaq()
    mgr = _Mgr([daq])
    st = live_tunnel_state(mgr)
    ref = tunnel_state(PDIFF, PTOT, TEMP)
    assert st.valid and st.q_psi == ref.q_psi
    assert daq.latest_calls == 1


def test_missing_channel_degrades_to_none():
    # no Pdiff source anywhere
    assert live_tunnel_state(_Mgr([HeiseLike()])) is None
    # no streams at all
    assert live_tunnel_state(_Mgr([])) is None
    # a device that ERRORS on latest() degrades, not raises
    class Boom(NiLike):
        def latest(self):
            raise RuntimeError("serial glitch")
    assert live_tunnel_state(_Mgr([Boom(), HeiseLike()])) is None


def test_partial_read_still_serves_what_exists():
    vals = read_tunnel_conditions(_Mgr([HeiseLike()]))
    assert vals == {"Ptot": PTOT, "Temp": TEMP}
    assert set(TUNNEL_CONDITION_CHANNELS) - set(vals) == {"Pdiff"}


def test_forces_q_and_results_q_use_the_registry():
    """The Forces page's _q_psi and the Results panel's _q_live_psi both
    derive q from the cross-device sources."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    QApplication.instance() or QApplication([sys.argv[0]])
    from freestream.app.forces import ForcesPanel
    from freestream.app.results import ResultsPanel
    from freestream.config import FreestreamConfig

    class _RoleMgr(_Mgr):
        def by_role(self, role):
            return None

        @property
        def positioner(self):
            return None

    mgr = _RoleMgr([NiLike(), HeiseLike()])
    cfg = FreestreamConfig()
    ref = tunnel_state(PDIFF, PTOT, TEMP)
    forces = ForcesPanel(mgr, cfg)
    try:
        assert forces._q_psi() == ref.q_psi
    finally:
        forces.shutdown()
    results = ResultsPanel(mgr, cfg)
    try:
        assert results._q_live_psi() == ref.q_psi
    finally:
        results.shutdown()
