"""Data structures for the ATE balance interface.

Two layers, both deliberately mirroring existing projects so this package can
later be merged into ``wtdaq`` and its output consumed by ``Streamlined``
without translation:

* **Live layer** (mirrors ``wtdaq.core.data_buffer``): :class:`BalanceFrame`
  (one raw TMSD scan), :class:`MasterFrame` + :data:`FIELDS` +
  :class:`RingBuffer` (the merged, derived, thread-safe stream).

* **Reduced layer** (mirrors ``Streamlined`` ``utils/gui/models/case.py``):
  :class:`TunnelConditions` and :class:`TestCase`, plus :class:`ReducedPoint`
  for a single dwell-averaged test point.

Everything here is **raw balance-frame loads** — Fx/Fy/Fz/Mx/My/Mz in the
balance reference frame (X back, Y right, Z up), exactly what the load cells
resolve.  Wind-axis lift/drag/side only exist after the span-aware resolution
in the Freestream suite, which also owns coefficient reduction (reference
geometry, air density).  Wire-name mapping: Fz was "Lift", Fx was "Drag",
Fy was "Side", Mx was "Roll", My was "Pitch", Mz was "Yaw".
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np


# ═════════════════════════════════════════════════════════════════════════
#  LIVE LAYER
# ═════════════════════════════════════════════════════════════════════════

@dataclass
class BalanceFrame:
    """One TMSD scan straight off the wire (no reduction applied yet).

    ``loads`` is keyed by the balance-frame axes (``Fx``, ``Fy``, ``Fz``,
    ``Mx``, ``My``, ``Mz``) in the units the OGI streams; the wire's
    Lift/Pitch/Drag/... labels are translated away at the decode boundary
    (``protocol.loads_to_balance_named``).
    """
    timestamp: float = 0.0
    loads: Dict[str, float] = field(default_factory=dict)
    sync: int = 0

    @property
    def ordered(self) -> List[float]:
        from .protocol import BALANCE_AXES
        return [self.loads.get(a, 0.0) for a in BALANCE_AXES]


# Field layout of a merged/derived master frame.  Same spirit as
# ``wtdaq.core.data_buffer.FIELDS``, in balance-frame naming (X back,
# Y right, Z up) — no wind-frame words at this layer, because the wind
# resolution is span-dependent and owned by the Freestream suite.
FIELDS = (
    "t", "alpha", "beta",
    # Balance reference frame loads
    "Fx", "Fy", "Fz", "Mx", "My", "Mz",
    # Tunnel dynamic pressure (measured by the aux source, not derived)
    "Q",
    # Hardware sync flag
    "sync",
)


@dataclass
class MasterFrame:
    """A single merged acquisition frame pushed to the ring buffer."""
    t: float = 0.0
    alpha: float = 0.0
    beta: float = 0.0
    Fx: float = 0.0
    Fy: float = 0.0
    Fz: float = 0.0
    Mx: float = 0.0
    My: float = 0.0
    Mz: float = 0.0
    Q: float = 0.0
    sync: int = 0

    def as_dict(self) -> dict:
        return {f: getattr(self, f) for f in FIELDS}


class RingBuffer:
    """Pre-allocated numpy ring buffer with thread-safe push/tail/drain.

    Mirrors ``wtdaq.core.data_buffer.RingBuffer`` so a future merge is a drop-in.
    """

    def __init__(self, capacity: int = 200_000):
        self._capacity = capacity
        self._data: Dict[str, np.ndarray] = {
            f: np.zeros(capacity, dtype=np.float64) for f in FIELDS
        }
        self._head = 0
        self._count = 0
        self._drain_idx = 0
        self._lock = threading.Lock()

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def push(self, frame: MasterFrame) -> None:
        with self._lock:
            idx = self._head % self._capacity
            for f in FIELDS:
                self._data[f][idx] = getattr(frame, f)
            self._head += 1
            self._count = min(self._count + 1, self._capacity)

    def tail(self, n: int) -> Dict[str, np.ndarray]:
        """Return the last ``n`` frames as a dict of numpy arrays (copies)."""
        with self._lock:
            n = min(n, self._count)
            if n == 0:
                return {f: np.array([], dtype=np.float64) for f in FIELDS}
            head = self._head % self._capacity
            if head >= n:
                slc = slice(head - n, head)
                return {f: self._data[f][slc].copy() for f in FIELDS}
            result = {}
            for f in FIELDS:
                part1 = self._data[f][self._capacity - (n - head):]
                part2 = self._data[f][:head]
                result[f] = np.concatenate([part1, part2])
            return result

    def drain_chunk(self, chunk_size: int = 5000) -> Optional[Dict[str, np.ndarray]]:
        with self._lock:
            available = self._head - self._drain_idx
            if available <= 0:
                return None
            n = min(chunk_size, available)
            start = self._drain_idx % self._capacity
            end = (self._drain_idx + n) % self._capacity
            self._drain_idx += n
            if end > start:
                return {f: self._data[f][start:end].copy() for f in FIELDS}
            result = {}
            for f in FIELDS:
                part1 = self._data[f][start:]
                part2 = self._data[f][:end]
                result[f] = np.concatenate([part1, part2])
            return result

    def clear(self) -> None:
        with self._lock:
            self._head = 0
            self._count = 0
            self._drain_idx = 0


# ═════════════════════════════════════════════════════════════════════════
#  REDUCED LAYER  (mirrors Streamlined utils/gui/models/case.py)
# ═════════════════════════════════════════════════════════════════════════

# Numeric channels that get mean+std reduction during a dwell.
_REDUCE_FIELDS = (
    "Fx", "Fy", "Fz", "Mx", "My", "Mz",
    "Q",
)


@dataclass
class ReducedPoint:
    """A single dwell-averaged test point (one alpha/beta condition).

    ``means``/``stds`` are keyed by the channel names in :data:`_REDUCE_FIELDS`.
    """
    alpha: float = 0.0
    beta: float = 0.0
    n_samples: int = 0
    means: Dict[str, float] = field(default_factory=dict)
    stds: Dict[str, float] = field(default_factory=dict)

    def mean(self, name: str) -> float:
        return self.means.get(name, 0.0)

    def as_row(self) -> Dict[str, float]:
        """Flat dict suitable for a CSV row / table model."""
        row: Dict[str, Any] = {"alpha": self.alpha, "beta": self.beta,
                               "n_samples": self.n_samples}
        for k, v in self.means.items():
            row[k] = v
        for k, v in self.stds.items():
            row[f"{k}_std"] = v
        return row


@dataclass
class TunnelConditions:
    """Tunnel flow conditions (mirrors Streamlined ``TunnelConditions``)."""
    Q: np.ndarray = field(default_factory=lambda: np.array([]))       # dyn. press (Pa here)
    Q_mks: np.ndarray = field(default_factory=lambda: np.array([]))
    U_inf: np.ndarray = field(default_factory=lambda: np.array([]))   # m/s
    rho: np.ndarray = field(default_factory=lambda: np.array([]))     # kg/m^3
    T: np.ndarray = field(default_factory=lambda: np.array([]))       # C
    P_tot: np.ndarray = field(default_factory=lambda: np.array([]))   # Pa
    Re: np.ndarray = field(default_factory=lambda: np.array([]))
    Mach: np.ndarray = field(default_factory=lambda: np.array([]))

    @property
    def mean_Q(self) -> float:
        return float(np.mean(self.Q)) if len(self.Q) else 0.0

    @property
    def mean_Re(self) -> float:
        return float(np.mean(self.Re)) if len(self.Re) else 0.0

    @property
    def mean_Mach(self) -> float:
        return float(np.mean(self.Mach)) if len(self.Mach) else 0.0


@dataclass
class TestCase:
    """A wind tunnel test case (structure mirrors Streamlined ``TestCase``).

    Carries the **raw balance-frame loads** per dwell point — Fx/Fy/Fz/Mx/My/Mz
    in the balance axes (X back, Y right, Z up).  Wind-axis resolution and
    coefficients are formed downstream (Streamlined / Freestream) using the
    span configuration and reference geometry; naming them lift/drag here
    would bake in the full-span assumption.
    """
    id: str = field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str = ""
    filepath: Optional[Path] = None
    date: Optional[datetime] = None
    run_number: int = 0
    visible: bool = True
    color: str = "#0078d4"
    marker: str = "o"
    metadata: Dict[str, Any] = field(default_factory=dict)

    # Processed arrays
    alphas: np.ndarray = field(default_factory=lambda: np.array([]))
    betas: np.ndarray = field(default_factory=lambda: np.array([]))

    # Balance-frame forces / moments, one entry per dwell point
    Fx: np.ndarray = field(default_factory=lambda: np.array([]))
    Fy: np.ndarray = field(default_factory=lambda: np.array([]))
    Fz: np.ndarray = field(default_factory=lambda: np.array([]))
    Mx: np.ndarray = field(default_factory=lambda: np.array([]))
    My: np.ndarray = field(default_factory=lambda: np.array([]))
    Mz: np.ndarray = field(default_factory=lambda: np.array([]))

    tunnel_conditions: TunnelConditions = field(default_factory=TunnelConditions)

    def __post_init__(self):
        if not self.name and self.filepath:
            self.name = Path(self.filepath).stem

    @property
    def has_data(self) -> bool:
        return len(self.alphas) > 0

    @property
    def n_points(self) -> int:
        return int(self.alphas.size)

    def get_channel(self, name: str) -> np.ndarray:
        cmap = {
            "Fx": self.Fx, "Fy": self.Fy, "Fz": self.Fz,
            "Mx": self.Mx, "My": self.My, "Mz": self.Mz,
            # legacy wire-name aliases (pre-rename callers / archives)
            "Lift": self.Fz, "Drag": self.Fx, "Side": self.Fy,
            "Roll": self.Mx, "Pitch": self.My, "Yaw": self.Mz,
            "alpha": self.alphas, "Alpha": self.alphas,
            "beta": self.betas, "Beta": self.betas,
        }
        return cmap.get(name, np.array([]))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id, "name": self.name,
            "filepath": str(self.filepath) if self.filepath else None,
            "run_number": self.run_number, "visible": self.visible,
            "color": self.color, "marker": self.marker,
            "metadata": self.metadata,
            "mach_number": self.tunnel_conditions.mean_Mach,
            "reynolds_number": self.tunnel_conditions.mean_Re,
        }

    @classmethod
    def from_reduced_points(cls, points: List[ReducedPoint], *,
                            name: str = "", run_number: int = 0) -> "TestCase":
        """Assemble a Streamlined-shaped TestCase from dwell-averaged points."""
        def col(key: str) -> np.ndarray:
            return np.array([p.means.get(key, 0.0) for p in points], dtype=float)

        tc = TunnelConditions(Q=col("Q"))
        return cls(
            name=name, run_number=run_number,
            alphas=np.array([p.alpha for p in points], dtype=float),
            betas=np.array([p.beta for p in points], dtype=float),
            Fx=col("Fx"), Fy=col("Fy"), Fz=col("Fz"),
            Mx=col("Mx"), My=col("My"), Mz=col("Mz"),
            tunnel_conditions=tc,
        )
