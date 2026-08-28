"""Frame merging and dwell averaging for the ATE balance.

The OGI resolves the six load cells to force/moment components about the
virtual centre **in the balance reference frame** (X back, Y right, Z up):
Fx/Fy/Fz/Mx/My/Mz.  This standalone package deals in those raw components
only — span-aware wind-axis resolution and coefficient reduction (reference
geometry, air density) live in the Freestream suite / Streamlined tooling.
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
        Fx=bf.loads.get("Fx", 0.0), Fy=bf.loads.get("Fy", 0.0),
        Fz=bf.loads.get("Fz", 0.0), Mx=bf.loads.get("Mx", 0.0),
        My=bf.loads.get("My", 0.0), Mz=bf.loads.get("Mz", 0.0),
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
