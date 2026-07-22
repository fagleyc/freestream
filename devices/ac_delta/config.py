"""Runtime configuration for the ARC Crescent (SSWT sting) drive.

Two Delta C2000 drives over Modbus TCP, one per axis:

| Axis  | IP           | Stop value | Direction sense |
|-------|--------------|------------|-----------------|
| Alpha | 192.168.1.11 | 17         | normal          |
| Beta  | 192.168.1.12 | 33         | inverted        |

Motion model (from the deployed C# ``HwControllerStingSSWT``): the host
runs the position loop — read encoder (reg 8714), pick one of five discrete
speed steps by remaining distance, write it to reg 8193, stop inside the
tolerance band. This driver keeps that model but with persistent
connections, change-only writes, a faster default loop, and configurable
deceleration bands (the C# hard-coded 100 ms and bands 1.0/1.5/2.25/3.0°).

Angle calibration is the C# two-point form:
``angle = angle_high − (encoder_high − encoder) / clicks_per_degree``.
The real constants live in the rig's XML tool config — enter them in the
Calibration tab (or capture two points) before trusting angles.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class AxisConfig:
    """One crescent axis (one Delta C2000 drive)."""
    name: str = "Alpha"
    ip: str = "192.168.1.11"
    port: int = 502
    unit_id: int = 1

    stop_value: int = 17            # 17 = Alpha, 33 = Beta
    invert_direction: bool = False  # Beta's fwd/rev tables are swapped

    # ── two-point angle calibration ──
    angle_high: float = 0.0
    encoder_high: int = 0
    clicks_per_degree: float = 100.0
    calibrated: bool = False        # warn until real constants are entered

    # ── motion ──
    min_deg: float = -20.0          # generic fallback — the rig limits are
    max_deg: float = 20.0           # set by the _alpha()/_beta() factories
    tolerance_deg: float = 0.01     # move-complete band

    def encoder_to_angle(self, encoder: int) -> float:
        return self.angle_high - ((self.encoder_high - encoder) /
                                  self.clicks_per_degree)

    def angle_to_encoder(self, angle: float) -> int:
        return round(self.encoder_high -
                     (self.angle_high - angle) * self.clicks_per_degree)

    # ── calibration routines ─────────────────────────────────────────────
    def calibrate_two_point(self, angle1: float, encoder1: int,
                            angle2: float, encoder2: int) -> float:
        """Full calibration: two (angle, encoder) points → slope AND offset.

        Returns the computed clicks/degree. Raises ValueError on
        degenerate points.
        """
        if abs(angle2 - angle1) < 1e-9:
            raise ValueError("calibration points must be at different angles")
        if encoder1 == encoder2:
            raise ValueError("encoder did not change between points")
        self.clicks_per_degree = (encoder2 - encoder1) / (angle2 - angle1)
        self.angle_high = float(angle2)
        self.encoder_high = int(encoder2)
        self.calibrated = True
        return self.clicks_per_degree

    def calibrate_offset(self, angle: float, encoder: int) -> None:
        """Offset-only re-zero: current position = known angle.

        Keeps the existing slope (clicks/degree must already be valid).
        """
        if abs(self.clicks_per_degree) < 1e-9:
            raise ValueError("slope unknown — run the two-point "
                             "calibration first")
        self.angle_high = float(angle)
        self.encoder_high = int(encoder)
        self.calibrated = True


# Measured slopes from the on-rig two-point calibration (2026-07-06).
# The offset still needs a limit-switch re-zero each setup, so axes start
# uncalibrated — but routine 2 (single point) is all that's needed.
ALPHA_CLICKS_PER_DEG = 294.8292
BETA_CLICKS_PER_DEG = 202.9586


def _alpha() -> AxisConfig:
    """Alpha axis rig defaults: travel ±29.0°, 0.01° settle band."""
    return AxisConfig(name="Alpha", ip="192.168.1.11", stop_value=17,
                      invert_direction=False,
                      clicks_per_degree=ALPHA_CLICKS_PER_DEG,
                      min_deg=-29.0, max_deg=29.0)


def _beta() -> AxisConfig:
    """Beta axis rig defaults: travel ±25.0°, 0.01° settle band."""
    return AxisConfig(name="Beta", ip="192.168.1.12", stop_value=33,
                      invert_direction=True,
                      clicks_per_degree=BETA_CLICKS_PER_DEG,
                      min_deg=-25.0, max_deg=25.0)


@dataclass
class CrescentConfig:
    """All user-tunable settings for the dual-axis drive."""

    alpha: AxisConfig = field(default_factory=_alpha)
    beta: AxisConfig = field(default_factory=_beta)

    # ── control loop ─────────────────────────────────────────────────────
    loop_ms: int = 50               # position-loop period (C# used 100)
    # Distance thresholds (deg) for speed steps 1..4; beyond the last one
    # the drive runs at step 5. Tighter values brake later (faster moves).
    # Defaults follow Casey's LabVIEW count bands (100/200/400/500 counts)
    # at the Alpha slope 294.83 clicks/deg ≈ [0.34, 0.68, 1.36, 1.70]°;
    # the C# used a much earlier-braking [1.0, 1.5, 2.25, 3.0].
    speed_bands_deg: List[float] = field(
        default_factory=lambda: [0.35, 0.7, 1.4, 1.7])
    max_step: int = 5               # cap the top speed step (1..5)

    # ── modbus ───────────────────────────────────────────────────────────
    modbus_timeout_s: float = 1.0
    max_consecutive_errors: int = 5  # watchdog: stop all + drop on breach

    # ── behaviour / display ──────────────────────────────────────────────
    force_sim: bool = False
    plot_window_s: float = 60.0

    def axes(self) -> List[AxisConfig]:
        return [self.alpha, self.beta]

    # ── serialization ────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CrescentConfig":
        d = dict(d)
        known = {f for f in cls.__dataclass_fields__}      # noqa: E1101
        ax_fields = {f for f in AxisConfig.__dataclass_fields__}  # noqa: E1101

        def mk_axis(a: Optional[dict], default) -> AxisConfig:
            if not a:
                return default()
            return AxisConfig(**{k: v for k, v in a.items()
                                 if k in ax_fields})

        cfg = cls(alpha=mk_axis(d.pop("alpha", None), _alpha),
                  beta=mk_axis(d.pop("beta", None), _beta),
                  **{k: v for k, v in d.items() if k in known and
                     k not in ("alpha", "beta")})
        return cfg

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path) -> "CrescentConfig":
        return cls.from_dict(json.loads(Path(path).read_text(
            encoding="utf-8")))
