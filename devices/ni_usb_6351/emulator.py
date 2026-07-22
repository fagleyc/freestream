"""Synthetic signals for hardware-free development and tests.

Balance bridge channels at wind-off: small offsets with slow thermal drift
and microvolt-level noise (post-conditioning, so a few mV at the DAQ).
Excitation reads steady 10 V; extra channels get a slow sine so live plots
visibly move.
"""

from __future__ import annotations

from typing import Dict, List

import numpy as np

from .config import ChannelConfig


class SimCore:
    def __init__(self, channels: List[ChannelConfig],
                 excitation_v: float = 10.0, seed: int = 20260717):
        self._channels = channels
        self._exc = excitation_v
        self._rng = np.random.default_rng(seed)
        # per-channel static offset (bridge imbalance), a few % of range.
        # Keyed by the PHYSICAL channel number (stable) so a live
        # balance-layout rename (N1↔AftPitch …) never orphans an offset.
        self._offsets = {c.channel: self._rng.uniform(-0.05, 0.05) *
                         c.native_range for c in channels}

    def block(self, t0: float, n: int, dt: float) -> Dict[str, np.ndarray]:
        """{channel_name: volts at the DAQ input} for n scans from t0."""
        t = t0 + np.arange(n) * dt
        out: Dict[str, np.ndarray] = {}
        for ch in self._channels:
            if ch.name == "Excitation":
                v = self._exc + self._rng.normal(0, 2e-4, n)
            elif ch.balance:
                drift = 2e-4 * np.sin(2 * np.pi * 0.002 * t + ch.channel)
                noise = self._rng.normal(0.0, 5e-5, n)   # ~50 µV RMS
                v = self._offsets[ch.channel] + drift + noise
            else:
                # extra channel: slow sine at 20% of range so plots move
                v = (0.2 * ch.native_range *
                     np.sin(2 * np.pi * 0.2 * t + ch.channel) +
                     self._rng.normal(0.0, 1e-4, n))
            out[ch.name] = v
        return out
