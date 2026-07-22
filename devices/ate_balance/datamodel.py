"""Data structures for the ATE balance interface.

Two layers, both deliberately mirroring existing projects so this package can
later be merged into ``wtdaq`` and its output consumed by ``Streamlined``
without translation:

* **Live layer** (mirrors ``wtdaq.core.data_buffer``): :class:`BalanceFrame`
  (one raw TMSD scan), :class:`MasterFrame` + :data:`FIELDS` +
  :class:`RingBuffer` (the merged, derived, thread-safe stream).

* **Reduced layer** (mirrors ``Streamlined`` ``utils/gui/models/case.py``):
  :class:`TunnelConditions` and :class:`TestCase`, plus :class:`ReducedPoint`
  for a single dwell-averaged test point.  Everything here is **raw wind-axis
  loads** (N, N·m) — aerodynamic coefficient reduction (reference geometry,
  air density) is owned by the Freestream suite, not this package.
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

    ``loads`` holds the six wire-order values (Lift, Pitch, Drag, Side, Yaw,
    Roll) in N and N.m, exactly as the OGI transmits them.
    """
    timestamp: float = 0.0
    loads: Dict[str, float] = field(default_factory=dict)
    sync: int = 0

    @property
    def ordered(self) -> List[float]:
        from .protocol import WIRE_AXES
        return [self.loads.get(a, 0.0) for a in WIRE_AXES]


# Field layout of a merged/derived master frame.  Same spirit as
# ``wtdaq.core.data_buffer.FIELDS`` but in wind-axis / Streamlined naming
# (the ATE/OGI already resolves loads to the wind frame about the virtual
# centre, so no body->wind transform is applied here).
FIELDS = (
    "t", "alpha", "beta",
    # Wind reference frame loads (N, N.m)
    "Lift", "Drag", "Side", "Roll", "Pitch", "Yaw",
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
    Lift: float = 0.0
    Drag: float = 0.0
    Side: float = 0.0
    Roll: float = 0.0
    Pitch: float = 0.0
    Yaw: float = 0.0
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
    "Lift", "Drag", "Side", "Roll", "Pitch", "Yaw",
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
    """A wind tunnel test case (subset-faithful mirror of Streamlined ``TestCase``).

    Field *names* match Streamlined exactly so a case assembled here drops
    straight into the Streamlined GUI / reduction tooling.  This package
    carries only the **raw wind-axis loads** — Streamlined/Freestream forms
    the coefficients from them using its own reference geometry.
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

    # WRF forces / moments (N, N.m on this rig)
    lift_forces: np.ndarray = field(default_factory=lambda: np.array([]))
    drag_forces: np.ndarray = field(default_factory=lambda: np.array([]))
    side_forces: np.ndarray = field(default_factory=lambda: np.array([]))
    roll_moments: np.ndarray = field(default_factory=lambda: np.array([]))
    pitch_moments: np.ndarray = field(default_factory=lambda: np.array([]))
    yaw_moments: np.ndarray = field(default_factory=lambda: np.array([]))

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
            "Lift": self.lift_forces, "Drag": self.drag_forces,
            "Side": self.side_forces, "Roll": self.roll_moments,
            "Pitch": self.pitch_moments, "Yaw": self.yaw_moments,
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
            lift_forces=col("Lift"), drag_forces=col("Drag"),
            side_forces=col("Side"), roll_moments=col("Roll"),
            pitch_moments=col("Pitch"), yaw_moments=col("Yaw"),
            tunnel_conditions=tc,
        )
