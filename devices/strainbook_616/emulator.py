"""Synthetic bridge signals for DLL-free development and tests.

Balance bridges at wind-off: small offsets with slow thermal drift and
microvolt-level noise; excitation readback steady at the external supply
voltage (default 10 V — the driver never commands internal excitation).
"""

from __future__ import annotations

from typing import Dict, List

import numpy as np

from .config import StrainChannelConfig


class SimCore:
    def __init__(self, channels: List[StrainChannelConfig],
                 excitation_v: float = 10.0, seed: int = 20260706):
        self._channels = channels
        self._exc = excitation_v
        self._rng = np.random.default_rng(seed)
        # per-channel static offset (bridge imbalance), a few % of range.
        # Keyed by the PHYSICAL channel number (stable) so a live
        # balance-layout rename (N1↔AftPitch …) never orphans an offset.
        self._offsets = {c.channel: self._rng.uniform(-0.1, 0.1) *
                         (c.range_mv / 1000.0) for c in channels}

    def block(self, t0: float, n: int, dt: float) -> Dict[str, np.ndarray]:
        """{channel_name: input-referred volts} for n scans from t0."""
        t = t0 + np.arange(n) * dt
        out: Dict[str, np.ndarray] = {}
        for ch in self._channels:
            if ch.read_excitation:
                # excitation readback reports the external supply verbatim
                # (config scale 1, offset 0)
                v = self._exc + self._rng.normal(0, 2e-4, n)
            else:
                drift = 2e-5 * np.sin(2 * np.pi * 0.002 * t + ch.channel)
                noise = self._rng.normal(0.0, 3e-6, n)   # ~3 µV RMS
                v = self._offsets[ch.channel] + drift + noise
            out[ch.name] = v
        return out
