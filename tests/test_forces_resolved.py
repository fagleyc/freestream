"""Forces page — resolved-load (external balance) element-load bars.

A fake external balance exposing ``load_limits`` + ``latest()`` resolved
loads drives the SAME LoadBar row the calibrated path uses:

* bars labelled with the REAL channel names (Lift, Drag, Side, Pitch,
  Yaw, Roll), utilization = |load|/max where a rated max exists;
* channels without a rated max (0/missing) show the live load VALUE in
  the pct label with an empty/neutral bar — never a fake utilization;
* overstress (any |load| >= max) raises the alarm banner + the record
  blocker exactly like the calibrated path, and DECAYS the same way;
* peak-hold markers ride ``_rolling_peak`` and reset on tare
  (``zero_count`` change).

Offscreen, no hardware.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import sys
from pathlib import Path
from typing import Dict, List

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication               # noqa: E402

from freestream.config import FreestreamConfig         # noqa: E402
from freestream.hal import ChannelSpec, Streaming      # noqa: E402
from freestream.app.forces import ForcesPanel          # noqa: E402

NAMES = ("Lift", "Pitch", "Drag", "Side", "Yaw", "Roll")


class FakeExternalBalance:
    """Streaming-protocol fake: resolved loads + load_limits, NO
    raw_tail (so the panel takes the resolved path) and NO zero()."""

    def __init__(self):
        self.id = "ate"
        self.label = "fake ATE"
        self.loads: Dict[str, float] = {n: 0.0 for n in NAMES}
        self.load_limits: Dict[str, float] = {}
        self.zero_count = 0

    def start(self):
        pass

    def stop(self):
        pass

    def channels(self) -> List[ChannelSpec]:
        return [ChannelSpec(name=n, unit=("N" if n in ("Lift", "Drag",
                                                       "Side") else "N*m"),
                            group="ATE_Balance", kind="raw", device_id="ate")
                for n in NAMES]

    def latest(self) -> Dict[str, float]:
        return dict(self.loads)

    def drain_block(self):
        return {n: np.zeros(1) for n in NAMES}

    def sample_rate(self) -> float:
        return 50.0


class FakeManager:
    def __init__(self, balance):
        self._balance = balance
        self.streaming = [balance]
        self.positioner = None
        self.extra_blockers = []

    def by_role(self, role):
        return self._balance if role == "balance" else None


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


@pytest.fixture()
def panel(app):
    bal = FakeExternalBalance()
    assert isinstance(bal, Streaming)      # protocol check, like _discover
    cfg = FreestreamConfig(operator="pytest")
    p = ForcesPanel(FakeManager(bal), cfg)
    p.active = True
    yield p, bal
    p.shutdown()


def test_resolved_bars_show_utilization_against_load_limits(panel):
    p, bal = panel
    bal.load_limits = {"Lift": 100.0, "Drag": 50.0}
    bal.loads.update({"Lift": 50.0, "Drag": -10.0, "Side": 12.3})
    p._sample()
    # bars labelled with the REAL channel names, in the display order
    labels = [p.util_labels[i].text() for i in range(6)]
    assert labels == ["Lift", "Drag", "Side", "Pitch", "Yaw", "Roll"]
    # Lift: 50/100 → 50 %
    i_lift = labels.index("Lift")
    assert p.util_bars[i_lift]._u == pytest.approx(0.5)
    assert p.util_bars[i_lift]._pct.text().strip() == "50.0%"
    # Drag: |−10|/50 → 20 % (absolute value)
    i_drag = labels.index("Drag")
    assert p.util_bars[i_drag]._u == pytest.approx(0.2)
    # Side: NO limit → honest value in the label, no bar fill
    i_side = labels.index("Side")
    assert p.util_bars[i_side]._u is None
    assert p.util_bars[i_side]._pct.text() == "+12.3 N"
    # tiles carry the resolved loads under their true names
    assert p.tiles["Lift"].value.text().startswith("+50")
    assert not p.overstress
    assert p.record_blocker() is None
    # external balance needs no .vol
    assert "no .vol needed" in p.info.text()


def test_resolved_overstress_blocks_and_decays(panel):
    p, bal = panel
    bal.load_limits = {"Lift": 100.0}
    bal.loads["Lift"] = 120.0                       # 120 % → overstress
    p._sample()
    assert p.overstress
    assert not p.alarm.isHidden()
    assert "OVERSTRESS" in p.alarm.text() and "Lift" in p.alarm.text()
    assert "OVERSTRESS" in (p.record_blocker() or "")
    # decay: load drops back below the rated max → blocker clears
    bal.loads["Lift"] = 10.0
    p._sample()
    assert not p.overstress
    assert p.alarm.isHidden()
    assert p.record_blocker() is None


def test_resolved_warn_band_banner(panel):
    p, bal = panel
    bal.load_limits = {"Yaw": 10.0}
    bal.loads["Yaw"] = 9.0                          # 90 % ≥ warn (80 %)
    p._sample()
    assert not p.overstress                         # not blocked …
    assert not p.alarm.isHidden()                   # … but warned
    assert "approaching limit" in p.alarm.text()


def test_resolved_peak_hold_and_tare_reset(panel):
    p, bal = panel
    bal.load_limits = {"Lift": 100.0}
    bal.loads["Lift"] = 80.0
    p._sample()
    bal.loads["Lift"] = 20.0
    p._sample()
    i_lift = [p.util_labels[i].text() for i in range(6)].index("Lift")
    bar = p.util_bars[i_lift]
    assert bar._u == pytest.approx(0.2)
    assert bar._peak == pytest.approx(0.8)          # rolling peak held
    # tare (zero_count change on the adapter) resets the peak history
    bal.zero_count += 1
    p._sample()
    assert p._peak_hist.get("Lift") is None or \
        max(v for _t, v in p._peak_hist["Lift"]) == pytest.approx(0.2)


def test_resolved_path_inactive_decays_alarm(panel):
    p, bal = panel
    bal.load_limits = {"Lift": 100.0}
    bal.loads["Lift"] = 150.0
    p._sample()
    assert p.overstress
    p.active = False                                # monitors idle
    p._sample()
    assert not p.overstress                         # stale blocker decayed
    assert p.record_blocker() is None


# ── unit labels come from the balance, not from a constant ─────────────
def test_tiles_relabel_from_the_balance_channel_units(panel):
    """The tiles used to be hardcoded lb / in·lb, which is right for a
    .vol reduction and wrong for an external balance streaming whatever
    the OGI's Units menu is set to."""
    p, bal = panel
    bal.loads.update({"Lift": 12.0, "Pitch": 3.0})
    p._sample()
    assert p.tiles["Lift"].unit.text() == "N"
    assert p.tiles["Pitch"].unit.text() == "N·m"      # N*m prettified

    # flip the balance to pounds and the tiles follow on the next tick
    def pounds():
        from freestream.hal import ChannelSpec as CS
        return [CS(name=n, unit=("lbf" if n in ("Lift", "Drag", "Side")
                                 else "lbf*ft"),
                   group="ATE_Balance", kind="raw", device_id="ate")
                for n in NAMES]
    bal.channels = pounds
    p._sample()
    assert p.tiles["Lift"].unit.text() == "lbf"
    assert p.tiles["Pitch"].unit.text() == "lbf·ft"


def test_display_filter_never_touches_the_recorded_stream(panel):
    """The monitor low-pass reads a display-only tail. drain_block — what
    the recorder consumes — must be unaffected by it."""
    p, bal = panel
    drained = []
    real_drain = bal.drain_block
    bal.drain_block = lambda: (drained.append(1), real_drain())[1]
    p.config.display_lpf_hz = 1.0
    for _ in range(3):
        p._sample()
    assert not drained, "the monitor consumed the recorder's samples"


def test_display_filter_uses_the_tail_when_one_is_offered(panel):
    """With a display_tail the tiles show a filtered mean, not the last
    raw frame — that is the whole point of the 1 Hz monitor filter."""
    p, bal = panel
    tail = np.concatenate([np.full(40, 10.0), np.full(10, 110.0)])
    bal.display_tail = lambda n: {"Lift": tail[-n:]}
    bal.loads["Lift"] = 110.0            # newest frame is the spike
    p.config.display_lpf_hz = 1.0
    p._sample()
    shown = float(p.tiles["Lift"].value.text().replace(",", ""))
    assert 10.0 < shown < 110.0, shown   # smoothed, not the raw spike
