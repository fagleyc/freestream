"""Synthetic tunnel signals for DLL-free development and tests.

Generates plausible subsonic-tunnel voltages for the standard channel set:
a slowly breathing dynamic pressure, a steady total pressure, and a slowly
drifting temperature, all with realistic noise.  Any extra channels get a
generic low-amplitude waveform so custom configs still exercise the GUI.
"""

from __future__ import annotations

from typing import Dict, List

import numpy as np

from .config import ChannelConfig


class SimCore:
    """Time-parametric voltage source; stateless between calls except RNG."""

    def __init__(self, channels: List[ChannelConfig], seed: int = 20260706):
        self._channels = channels
        self._rng = np.random.default_rng(seed)

    def block(self, t0: float, n: int, dt: float) -> Dict[str, np.ndarray]:
        """Return {channel_name: volts array} for scans at t0, t0+dt, ..."""
        t = t0 + np.arange(n) * dt
        out: Dict[str, np.ndarray] = {}
        for ch in self._channels:
            name = ch.name
            if name == "Pdiff":
                # tunnel coming up to speed and "breathing" slightly
                v = (1.30 + 0.12 * np.sin(2 * np.pi * 0.05 * t)
                     + 0.02 * np.sin(2 * np.pi * 0.8 * t)
                     + self._rng.normal(0.0, 0.004, n))
            elif name == "Ptot":
                v = (6.40 + 0.05 * np.sin(2 * np.pi * 0.05 * t + 0.7)
                     + self._rng.normal(0.0, 0.006, n))
            elif name == "Temp":
                v = (2.95 + 0.10 * np.sin(2 * np.pi * 0.01 * t)
                     + self._rng.normal(0.0, 0.003, n))
            else:
                v = (0.5 + 0.2 * np.sin(2 * np.pi * 0.2 * t + ch.channel)
                     + self._rng.normal(0.0, 0.01, n))
            lo, hi = ch.v_min, ch.v_max
            out[name] = np.clip(v, lo, hi)
        return out
