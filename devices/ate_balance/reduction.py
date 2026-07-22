"""Frame merging and dwell averaging for the ATE balance.

The ATE/OGI already resolves the six loads to the **wind reference frame**
about the virtual centre (Lift, Pitch, Drag, Side, Yaw, Roll, in N and N.m),
and this standalone package deals in those raw loads only — aerodynamic
coefficient reduction (reference geometry, air density) lives in the
Freestream suite / Streamlined tooling, not here.
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np

from .datamodel import (BalanceFrame, MasterFrame, ReducedPoint, _REDUCE_FIELDS)


def build_master_frame(bf: BalanceFrame, *, alpha: float, beta: float,
                       q_dyn: float) -> MasterFrame:
    """Merge one balance scan + attitude + tunnel q into a MasterFrame."""
    return MasterFrame(
        t=bf.timestamp, alpha=alpha, beta=beta,
        Lift=bf.loads.get("Lift", 0.0), Drag=bf.loads.get("Drag", 0.0),
        Side=bf.loads.get("Side", 0.0), Roll=bf.loads.get("Roll", 0.0),
        Pitch=bf.loads.get("Pitch", 0.0), Yaw=bf.loads.get("Yaw", 0.0),
        Q=q_dyn, sync=bf.sync,
    )


# ─────────────────────────────────────────────────────────────────────────
#  Dwell averaging  (mirrors wtdaq SyncManager.begin_dwell/end_dwell)
# ─────────────────────────────────────────────────────────────────────────

class DwellAccumulator:
    """Collect MasterFrames between begin()/end() and reduce to a ReducedPoint."""

    def __init__(self) -> None:
        self._active = False
        self._frames: List[MasterFrame] = []
        self._alpha = 0.0
        self._beta = 0.0

    @property
    def active(self) -> bool:
        return self._active

    @property
    def n(self) -> int:
        return len(self._frames)

    def begin(self, alpha: float, beta: float) -> None:
        self._active = True
        self._frames = []
        self._alpha = alpha
        self._beta = beta

    def add(self, frame: MasterFrame) -> None:
        if self._active:
            self._frames.append(frame)

    def cancel(self) -> None:
        self._active = False
        self._frames = []

    def end(self) -> Optional[ReducedPoint]:
        self._active = False
        frames = self._frames
        self._frames = []
        if not frames:
            return None
        means: Dict[str, float] = {}
        stds: Dict[str, float] = {}
        for name in _REDUCE_FIELDS:
            vals = np.array([getattr(f, name) for f in frames], dtype=float)
            means[name] = float(np.mean(vals))
            stds[name] = float(np.std(vals))
        return ReducedPoint(alpha=self._alpha, beta=self._beta,
                            n_samples=len(frames), means=means, stds=stds)
