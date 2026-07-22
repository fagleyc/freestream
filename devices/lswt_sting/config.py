"""Runtime configuration for the LSWT Sting drive.

Two SLO-SYN/Compumotor-style serial stepper indexers daisy-chained on one
RS-232 port (9600-8N1, CR-terminated), one per axis:

| Axis  | Unit | Steps/degree | Accel/Decel | Velocity |
|-------|------|--------------|-------------|----------|
| Alpha | 1    | 2741.0525    | 10.8528     | 0.108    |
| Beta  | 2    | 66.8         | 2           | 0.032    |

All constants were recovered from the deployed C# ``Tool_LSWT_Sting``
(``Core.HwControllerStingLSWT_HWside``): commands are ``<unit><cmd>``
(``1R``, ``2G`` …); moves are relative distances in motor steps (``D`` then
``G``); position is the indexer's step counter (``PR``), zeroed with ``PZ``.

Position model (open loop)
--------------------------
There is no encoder — angle is tracked by the indexer step counter. The
counter zero is established by the operator: physically set the sting to a
known angle, enter it, and Zero the axis (``PZ``). Until an axis is zeroed
(``zeroed`` flag), absolute angle moves are refused; only jog steps are
allowed. Angle = ``zero_offset_deg + counts / steps_per_degree``.

Safety
------
Soft travel limits are enforced host-side before any command (the legacy
limits live in the rig's "Tunnel Default File" — confirm on first live
run). A ``*S`` (stall) response latches a fault that stops everything and
requires an explicit operator reset; the drives report stall when the
motor cannot follow the commanded profile.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

# steps-per-degree constants hard-coded in the deployed C# tool
ALPHA_STEPS_PER_DEG = 2741.052490234375
BETA_STEPS_PER_DEG = 66.80000305175781

#: motor steps per revolution (indexer microstep setting); used only to
#: estimate move duration for the timeout watchdog
DEFAULT_STEPS_PER_REV = 25_000


@dataclass
class StingAxisConfig:
    """One sting axis (one serial indexer unit)."""
    name: str = "Alpha"
    unit: str = "1"                 # daisy-chain unit number prefix
    enabled: bool = True

    steps_per_degree: float = ALPHA_STEPS_PER_DEG
    steps_per_rev: int = DEFAULT_STEPS_PER_REV

    #: sign linking angle to the indexer step counter:
    #: ``counts = direction * (angle - zero_offset) * steps_per_degree``.
    #: FIELD-VERIFIED 2026-07-22 on the rig: Alpha = -1 (positive angle
    #: = negative counts; Core.exe IL agrees — ``SetStingToAngle``
    #: multiplies by -2741.0525 and the readback negates the counter),
    #: Beta = +1 (observed backwards with -1; the legacy
    #: ``MoveStingByDegrees`` Beta branch, which omits the negation,
    #: had the physically correct sign — its absolute path carried the
    #: dormant sign bug). Getting a sign wrong drives the axis into the
    #: hard stop (Alpha stall, 2026-07-22).
    direction: int = -1

    # motion parameters pushed at connect (legacy values; drive units,
    # rev/s and rev/s^2). Stored as strings exactly as transmitted so the
    # on-wire commands match the deployed tool byte-for-byte.
    acceleration: str = "10.8528"
    deceleration: str = "10.8528"
    velocity: str = ".108"

    # ── open-loop zero reference ──
    zero_offset_deg: float = 0.0    # angle at indexer counter zero
    zeroed: bool = False            # absolute moves refused until zeroed

    # ── travel limits (host-side; from the Tunnel Default File) ──
    min_deg: float = -15.0
    max_deg: float = 30.0
    tolerance_deg: float = 0.05     # reporting band only (indexer settles)

    def counts_to_angle(self, counts: int) -> float:
        return (self.zero_offset_deg
                + self.direction * counts / self.steps_per_degree)

    def angle_to_counts(self, angle: float) -> int:
        return round(self.direction * (angle - self.zero_offset_deg)
                     * self.steps_per_degree)

    def velocity_deg_s(self) -> float:
        """Approximate axis speed in deg/s (for move-timeout estimation)."""
        try:
            v_rev_s = float(self.velocity)
        except ValueError:
            v_rev_s = 0.1
        return max(v_rev_s * self.steps_per_rev / self.steps_per_degree,
                   1e-3)


def _alpha() -> StingAxisConfig:
    """Alpha axis defaults (unit 1). Travel guess −15…+30° — the legacy
    limits come from the Tunnel Default File; park position is ~+29.3°."""
    return StingAxisConfig(name="Alpha", unit="1",
                           steps_per_degree=ALPHA_STEPS_PER_DEG,
                           acceleration="10.8528", deceleration="10.8528",
                           velocity=".108", min_deg=-15.0, max_deg=30.0)


def _beta() -> StingAxisConfig:
    """Beta axis defaults (unit 2). direction=+1 field-verified
    2026-07-22 (moves backwards with -1)."""
    return StingAxisConfig(name="Beta", unit="2",
                           steps_per_degree=BETA_STEPS_PER_DEG,
                           acceleration="2", deceleration="2",
                           velocity=".032", min_deg=-15.0, max_deg=15.0,
                           direction=+1)


@dataclass
class StingConfig:
    """All user-tunable settings for the LSWT sting session."""

    alpha: StingAxisConfig = field(default_factory=_alpha)
    beta: StingAxisConfig = field(default_factory=_beta)

    # ── serial ───────────────────────────────────────────────────────────
    com_port: str = "COM1"
    baud: int = 9600                # drives are fixed 9600-8N1, CR newline
    serial_timeout_s: float = 0.5
    init_reset: bool = True         # send Z (reset) during connect init,
                                    # exactly like the legacy InitHw

    # ── control/poll loop ────────────────────────────────────────────────
    poll_ms: int = 250              # status/position poll period
    move_timeout_margin: float = 1.5   # timeout = est. duration × margin+5 s
    max_consecutive_errors: int = 5    # serial watchdog → stop + drop

    # ── behaviour / display ──────────────────────────────────────────────
    park_on_disconnect: bool = True    # legacy parked Alpha at ~+29.3°;
    # ON by default — the sting has NO BRAKE, park is the rest position
    park_alpha_deg: float = 30.0       # rest against the +30° end
    force_sim: bool = False
    plot_window_s: float = 60.0

    # ── position persistence (no brake — safety) ─────────────────────────
    # The live angle is checkpointed to ``state_path`` continuously and
    # marked clean on orderly disconnect. On reconnect the zero
    # reference is restored so an abrupt shutdown does not lose the
    # position — with a loud warning to verify physically, since an
    # unpowered sting can droop.
    restore_position: bool = True
    state_path: str = ""               # default: <package>/sting_state.json

    def resolved_state_path(self) -> Path:
        if self.state_path:
            return Path(self.state_path)
        return Path(__file__).resolve().parent / "sting_state.json"

    def axes(self) -> List[StingAxisConfig]:
        return [self.alpha, self.beta]

    def enabled_axes(self) -> List[StingAxisConfig]:
        return [a for a in self.axes() if a.enabled]

    # ── serialization ────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StingConfig":
        d = dict(d)
        known = {f for f in cls.__dataclass_fields__}      # noqa: E1101
        ax_fields = {f for f in
                     StingAxisConfig.__dataclass_fields__}  # noqa: E1101

        def mk_axis(a: Optional[dict], default) -> StingAxisConfig:
            if not a:
                return default()
            return StingAxisConfig(**{k: v for k, v in a.items()
                                      if k in ax_fields})

        return cls(alpha=mk_axis(d.pop("alpha", None), _alpha),
                   beta=mk_axis(d.pop("beta", None), _beta),
                   **{k: v for k, v in d.items() if k in known and
                      k not in ("alpha", "beta")})

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path) -> "StingConfig":
        return cls.from_dict(json.loads(Path(path).read_text(
            encoding="utf-8")))
