"""Calibration session model.

Mirrors the MATLAB ForceCal app's data flow: a session holds the balance
metadata (description, max loads, element distances) plus, per load
orientation (``N1 pos`` … ``Mx neg``), the list of acquired test points.
Each test point is the applied load (dead weight x moment arm) and the
time-averaged voltage of the six bridge channels and the excitation
channel.

Element naming follows the .vol 3.1 example (``2025_06_06_2 100 lb.vol``):
the sixth element of a force balance is the rolling moment — section and
max-load name ``Mx``, bridge-channel name ``Roll``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Dict, List, Optional


class BalanceKind(Enum):
    FORCE = "Force Balance"          # 5-force / 1-moment
    MOMENT = "Moment Balance"        # 1-force / 5-moment

    @property
    def type_string(self) -> str:
        """Balance-type string written to the .vol header."""
        return ("5 Force/1 Moment" if self is BalanceKind.FORCE
                else "1 Force/5 Moment")


@dataclass(frozen=True)
class ElementDef:
    """One balance element (load direction) and its naming/units."""
    name: str            # section + max-load name in the .vol ("Mx", "N1"…)
    channel: str         # DAQ channel name ("Roll", "N1", "AftPitch"…)
    load_unit: str       # "lb" or "in-lb"
    distance_tag: str = ""   # "x1"/"x2"/"y1"/"y2" for the [Distances] block
    image_stem: str = ""     # FB_Cal_GUI guide-image stem ("N1", "Axial"…)

    def _stem(self) -> str:
        return self.image_stem or self.name


FORCE_ELEMENTS = (
    ElementDef("N1", "N1", "lb", "x1"),
    ElementDef("N2", "N2", "lb", "x2"),
    ElementDef("Y1", "Y1", "lb", "y1"),
    ElementDef("Y2", "Y2", "lb", "y2"),
    ElementDef("Ax", "Axial", "lb", "", "Axial"),
    ElementDef("Mx", "Roll", "in-lb", "", "Roll"),
)

MOMENT_ELEMENTS = (
    ElementDef("Aft_Pitch", "AftPitch", "in-lb", "x1", "Aft_Pitch"),
    ElementDef("Aft_Yaw", "AftYaw", "in-lb", "x2", "Aft_Yaw"),
    ElementDef("Fwd_Pitch", "FwdPitch", "in-lb", "y1", "Fwd_Pitch"),
    ElementDef("Fwd_Yaw", "FwdYaw", "in-lb", "y2", "Fwd_Yaw"),
    ElementDef("Ax", "Axial", "lb", "", "Axial"),
    ElementDef("Mx", "Roll", "in-lb", "", "Roll"),
)

#: Column header for the volt columns of each section, in channel order.
VOLT_COLUMNS_FORCE = ("N1", "N2", "Y1", "Y2", "Ax", "Roll")
VOLT_COLUMNS_MOMENT = ("Aft_Pitch", "Aft_Yaw", "Fwd_Pitch", "Fwd_Yaw",
                       "Ax", "Roll")


def elements_for(kind: BalanceKind):
    return FORCE_ELEMENTS if kind is BalanceKind.FORCE else MOMENT_ELEMENTS


def volt_columns_for(kind: BalanceKind):
    return (VOLT_COLUMNS_FORCE if kind is BalanceKind.FORCE
            else VOLT_COLUMNS_MOMENT)


@dataclass(frozen=True)
class Orientation:
    """One loading direction of one element, e.g. ``N1 pos``."""
    element: ElementDef
    positive: bool

    @property
    def key(self) -> str:                       # "N1_pos" — UI / dict key
        return f"{self.element.name}_{'pos' if self.positive else 'neg'}"

    @property
    def section(self) -> str:                   # "[N1 pos]" section name
        return f"{self.element.name} {'pos' if self.positive else 'neg'}"

    @property
    def sign(self) -> int:
        return 1 if self.positive else -1

    def image_name(self, kind: "BalanceKind") -> str:
        """Guide-image filename in FB_Cal_GUI (e.g. ``FB_N1_pos.png``)."""
        prefix = "FB" if kind is BalanceKind.FORCE else "MB"
        suffix = "pos" if self.positive else "neg"
        return f"{prefix}_{self.element._stem()}_{suffix}.png"


def orientations_for(kind: BalanceKind) -> List[Orientation]:
    out: List[Orientation] = []
    for el in elements_for(kind):
        out.append(Orientation(el, True))
        out.append(Orientation(el, False))
    return out


@dataclass
class TestPoint:
    """One acquired point: applied load and mean channel voltages."""
    __test__ = False        # not a pytest class, despite the name
    load: float                      # dead weight x moment arm, load units
    volts: List[float]               # 6 bridge means [V], channel order
    excitation: float                # mean excitation [V]
    stds: Optional[List[float]] = None   # 6 bridge stddevs [V] — noise
    # quality indicator for freshly acquired points; None when the point
    # was reloaded from a .vol (the format does not store them)
    excluded: bool = False           # left out of the fit AND the .vol
    # (session-only flag — the .vol format cannot mark exclusions)

    def row(self) -> List[float]:
        return [self.load, *self.volts, self.excitation]


@dataclass
class CalSession:
    kind: BalanceKind = BalanceKind.FORCE
    operator: str = ""
    cal_date: date = field(default_factory=date.today)
    serial_number: str = ""
    outer_diameter: str = ""         # free text, e.g. "0.75 in."
    max_loads: Dict[str, float] = field(default_factory=dict)   # by element
    distances: Dict[str, float] = field(default_factory=dict)   # by tag x1…y2
    points: Dict[str, List[TestPoint]] = field(default_factory=dict)
    #: bridge DC tare captured unloaded at setup start [V]; subtracted
    #: from every acquired point's volts (excitation is never tared).
    #: Acquisition-time convenience only — recorded .vol volts are the
    #: net values, so nothing downstream needs to know about it.
    tare_volts: Optional[List[float]] = None

    @property
    def elements(self):
        return elements_for(self.kind)

    @property
    def orientations(self) -> List[Orientation]:
        return orientations_for(self.kind)

    def orientation(self, key: str) -> Orientation:
        for o in self.orientations:
            if o.key == key:
                return o
        raise KeyError(key)

    def add_point(self, key: str, point: TestPoint) -> None:
        self.orientation(key)                   # validate
        self.points.setdefault(key, []).append(point)

    def remove_point(self, key: str, index: int) -> None:
        self.points[key].pop(index)

    def active_points(self, key: str) -> List["TestPoint"]:
        return [p for p in self.points.get(key, []) if not p.excluded]

    def point_count(self) -> int:
        return sum(len(v) for v in self.points.values())

    def excluded_count(self) -> int:
        return sum(1 for v in self.points.values()
                   for p in v if p.excluded)

    def apply_tare(self, volts: List[float]) -> List[float]:
        """Measured bridge volts → recorded volts (tare subtracted)."""
        if self.tare_volts is None:
            return list(volts)
        return [v - t for v, t in zip(volts, self.tare_volts)]

    # ── moment-arm logic ─────────────────────────────────────────────────
    #: pitch elements share one load couple (aft x1 + fwd y1), yaw the
    #: other (x2 + y2) — the weight hangs at the opposite station, so
    #: the arm is the full station separation (see MB_*_pos.png).
    _ARM_PAIRS = {"x1": ("x1", "y1"), "y1": ("x1", "y1"),
                  "x2": ("x2", "y2"), "y2": ("x2", "y2")}

    def _pair_distances(self, tag: str):
        a, b = self._ARM_PAIRS[tag]
        return self.distances.get(a), self.distances.get(b)

    def moment_arm(self, key: str) -> float:
        """Multiplier applied to the entered dead weight for this
        orientation — converts the weight [lb] to the load actually seen
        by the gauge. 1.0 for direct force loads; for moment-balance
        pitch/yaw the weight hangs at the opposite element station, so
        the moment at the gauge is weight x (sum of both stations'
        distances to the balance center); roll uses the element-6
        distance (the roll bar arm, default 2 in).

        (The MATLAB app approximated the pitch/yaw arm as 2x a single
        distance, exact only for a symmetric balance — the pair sum is
        the general form.)"""
        o = self.orientation(key)
        if o.element.channel == "Roll":
            return self.distances.get("roll_arm", 2.0)
        if self.kind is BalanceKind.MOMENT and o.element.distance_tag:
            d1, d2 = self._pair_distances(o.element.distance_tag)
            if d1 is not None and d1 > 0 and d2 is not None and d2 > 0:
                return d1 + d2
        return 1.0

    def moment_arm_warning(self, key: str) -> Optional[str]:
        """Warn when a moment load needs distances that have not been
        entered (arm silently falls back to 1.0)."""
        o = self.orientation(key)
        if self.kind is BalanceKind.MOMENT and o.element.distance_tag:
            d1, d2 = self._pair_distances(o.element.distance_tag)
            if d1 is None or d1 <= 0 or d2 is None or d2 <= 0:
                return (f"Moment arm for {o.element.name} needs both "
                        f"station distances "
                        f"({' + '.join(self._ARM_PAIRS[o.element.distance_tag])}) "
                        f"— defaulting to 1.0")
        return None
