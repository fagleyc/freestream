"""Runtime configuration for the South LSWT traverse (IDC SmartStep23).

Three SmartStep23 SmartDrives on one RS-232C daisy chain (9600 8N1,
XON/XOFF), unit-addressed on the rig as drive 1 = Z vertical,
2 = Y lateral, 3 = X axial (photo ``stepper_drivers.jpg``). The drives
were configured for the actuators via the keypad / Application
Developer, so ``PA`` already answers in USER UNITS (inches) — no
counts calibration lives here, unlike the SWT WAGO traverse.

Referencing is deliberately simple (the whole point of this driver):
there is NO homing routine. The operator jogs each axis to its
reference spot, presses "Set home here" (wire: ``SPr`` with the datum,
normally 0), and the soft travel limits below then gate every
commanded move HOST-side. The reference is per-drive-power-cycle —
a SmartStep wakes up reading 0 wherever it stands — so each connect
starts unreferenced and absolute moves stay locked until a home is set.

Sign conventions (``traverse_actuators_annotated.jpg``): X + is
downstream toward the test section, Y + is right looking downstream,
Z + is up.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .protocol import UNIT_X, UNIT_Y, UNIT_Z


def defaults_path() -> Path:
    """Where "Set as Defaults" persists the startup config.

    Auto-loaded at every app launch (guarded — a parse error falls back
    to factory defaults). Overridable via the ``LSWT_TRAVERSE_DEFAULTS``
    env var (tests); default ``~/.lswt_traverse/defaults.json``.
    """
    env = os.environ.get("LSWT_TRAVERSE_DEFAULTS")
    return Path(env) if env else (Path.home() / ".lswt_traverse" /
                                  "defaults.json")


@dataclass
class AxisConfig:
    """One traverse axis (one SmartStep23 drive on the chain)."""
    name: str = "X"
    label: str = "Axial"
    unit: int = UNIT_X              # daisy-chain address (drive number)
    enabled: bool = True

    # ── soft travel limits, inches from the operator-set home ──────────
    # These are the "software limitations": HOST-side gates on every
    # absolute target and every jog. Travel spans are placeholders until
    # measured on the rig — tighten them there and Set as Defaults.
    min_in: float = -12.0
    max_in: float = 12.0

    # ── motion shaping (sent with every move: AC / DE / VE) ────────────
    velocity_ips: float = 1.0       # positioning speed, units/s
    jog_velocity_ips: float = 0.5   # hold-to-jog speed, units/s
    accel: float = 0.2              # AC/DE argument (drive accel units)
    tolerance_in: float = 0.01      # move-complete band

    # ── referencing datum ──────────────────────────────────────────────
    # "Set home here" sends SP<home_datum_in>; 0 = the reference spot IS
    # the origin. A nonzero datum lets the reference live at a known
    # offset (e.g. the mast parked against a physical stop at −10").
    home_datum_in: float = 0.0


def _x() -> AxisConfig:
    return AxisConfig(name="X", label="Axial", unit=UNIT_X,
                      min_in=-12.0, max_in=12.0)


def _y() -> AxisConfig:
    return AxisConfig(name="Y", label="Lateral", unit=UNIT_Y,
                      min_in=-12.0, max_in=12.0)


def _z() -> AxisConfig:
    return AxisConfig(name="Z", label="Vertical", unit=UNIT_Z,
                      min_in=-12.0, max_in=12.0)


@dataclass
class TraverseConfig:
    """All user-tunable settings for the South LSWT traverse."""

    # ── serial ──────────────────────────────────────────────────────────
    # One COM port serves the whole chain. Set to the real port on the
    # tunnel PC (Device Manager) and Set as Defaults.
    port: str = "COM1"
    force_sim: bool = False         # ignore serial, run the emulator

    #: per-transaction reply deadline (chain echo + response @ 9600 baud)
    serial_timeout_s: float = 1.0

    x: AxisConfig = field(default_factory=_x)
    y: AxisConfig = field(default_factory=_y)
    z: AxisConfig = field(default_factory=_z)

    # ── monitor loop ────────────────────────────────────────────────────
    # Each cycle polls PA + SA per axis over the shared 9600-baud line
    # (~6 transactions, ≥20 ms each with echo) — 0.25 s keeps headroom.
    poll_s: float = 0.25
    #: poll SD (drive faults) every N cycles (cheap enough, not urgent)
    drive_status_every: int = 8
    #: a commanded move may run at most this long before it is stopped
    move_timeout_s: float = 120.0

    # ── display ────────────────────────────────────────────────────────
    plot_window_s: float = 60.0

    def axis(self, name: str) -> AxisConfig:
        return {"x": self.x, "y": self.y, "z": self.z}[name.lower()]

    def axes(self) -> List[AxisConfig]:
        return [self.x, self.y, self.z]

    # ── serialization ──────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TraverseConfig":
        d = dict(d)
        for key in ("x", "y", "z"):
            if isinstance(d.get(key), dict):
                known = set(AxisConfig.__dataclass_fields__)
                d[key] = AxisConfig(**{k: v for k, v in d[key].items()
                                       if k in known})
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in d.items() if k in known})

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path) -> "TraverseConfig":
        return cls.from_dict(json.loads(
            Path(path).read_text(encoding="utf-8")))

    @classmethod
    def load_defaults(cls) -> "TraverseConfig":
        """Startup config: saved defaults if present and parseable,
        factory values otherwise (never raises)."""
        path = defaults_path()
        try:
            if path.is_file():
                return cls.load(path)
        except (OSError, ValueError, TypeError, KeyError):
            pass
        return cls()
