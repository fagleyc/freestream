"""Auxiliary measurement sources — the DAQbook integration point.

This pass implements the *structure* only (per the design decision): a small
:class:`AuxSource` interface plus a synthetic implementation.  Wiring the real
IOtech DAQbook/2000 in later is then a drop-in: implement :class:`AuxSource`
around the existing ``wtdaq.devices.daqbook2000.DAQbook2000`` driver and hand it
to the app — nothing else changes.

An aux source contributes named scalar channels (e.g. tunnel dynamic pressure
from a pitot transducer, temperature, barometric pressure) sampled alongside
the balance loads.  The app reads :meth:`latest` when it builds each merged
record / dwell-averaged point, exactly as ``wtdaq``'s SyncManager folds the
DAQbook's ``Q_DYN``/``PITOT`` channel into its MasterFrames.
"""

from __future__ import annotations

import abc
import time
from typing import Dict, List, Optional

import numpy as np


class AuxSource(abc.ABC):
    """Abstract source of extra named scalar channels sampled with the balance."""

    #: Channel name the app treats as tunnel dynamic pressure (Pa), if present.
    Q_CHANNEL = "Q_DYN"

    @property
    @abc.abstractmethod
    def name(self) -> str:
        ...

    @abc.abstractmethod
    def channel_labels(self) -> List[str]:
        """Ordered list of channel names this source provides."""

    @abc.abstractmethod
    def latest(self) -> Dict[str, float]:
        """Most recent value for each channel (engineering units)."""

    def start(self) -> None:
        """Begin acquisition (no-op for sources that are always live)."""

    def stop(self) -> None:
        """Stop acquisition."""

    def dynamic_pressure(self) -> Optional[float]:
        """Tunnel q (Pa) if this source exposes it, else ``None``."""
        vals = self.latest()
        for key, v in vals.items():
            if self.Q_CHANNEL in key.upper() or "PITOT" in key.upper():
                return float(v)
        return None


class SimAuxSource(AuxSource):
    """Synthetic aux source: a steady tunnel q plus temperature/baro channels.

    Stands in for a DAQbook until the real driver is wired.  ``q_pa`` lets the
    app demonstrate live coefficient reduction against a known dynamic pressure.
    """

    def __init__(self, q_pa: float = 500.0, seed: int = 7):
        self._q = float(q_pa)
        self._rng = np.random.default_rng(seed)

    @property
    def name(self) -> str:
        return "SimAux"

    def channel_labels(self) -> List[str]:
        return [self.Q_CHANNEL, "T_AIR", "P_BARO"]

    def latest(self) -> Dict[str, float]:
        return {
            self.Q_CHANNEL: self._q + float(self._rng.normal(0.0, 2.0)),
            "T_AIR": 20.0 + float(self._rng.normal(0.0, 0.1)),
            "P_BARO": 78000.0 + float(self._rng.normal(0.0, 20.0)),  # ~2200 m
        }


# ─────────────────────────────────────────────────────────────────────────
#  Real DAQbook wiring — left as a documented stub for the next pass.
# ─────────────────────────────────────────────────────────────────────────
#
# class DaqbookAuxSource(AuxSource):
#     """Wrap wtdaq's DAQbook/2000 driver as an AuxSource.
#
#     def __init__(self, driver):                # wtdaq.devices.daqbook2000.DAQbook2000
#         self._driver = driver
#         self._last: Dict[str, float] = {}
#         driver.on_frame = self._on_frame       # DAQbookFrame -> stash .values
#
#     def _on_frame(self, frame):
#         self._last = dict(frame.values)
#
#     def channel_labels(self):
#         return [c.label for c in self._driver.enabled_channels()]
#
#     def latest(self):
#         return dict(self._last)
#
#     def start(self): self._driver.connect(); self._driver.start()
#     def stop(self):  self._driver.stop();    self._driver.disconnect()
#
# The app already calls AuxSource.latest()/dynamic_pressure() when building each
# record, so swapping SimAuxSource -> DaqbookAuxSource is the only change needed.
