"""Runtime configuration for the North & South LSWT fan drives.

Each tunnel's fan runs on an **ABB ACS530 VFD** over Modbus TCP,
**unit/slave 1**. The register map (from the deployed C#
``Tool_LSWT_Flow_Velocity\\HwControllerVelocityLSWT_ACB530.cs``, which
used FieldTalk's 1-BASED register addressing → wire address =
FieldTalk − 1, same +1 lesson as this repo's other C# ports):

| wire | FieldTalk | meaning                                          |
|------|-----------|--------------------------------------------------|
| 0    | 1         | Control register: 1150 = STOP, 1151 = START      |
| 1    | 2         | Reference: 0–20000 scales 0–full speed (60 Hz).  |
|      |           | The C# wrote the NEGATIVE of the scaled value    |
| 102  | 103       | Actual output frequency × 10 (0–600 = 0–60.0 Hz) |

The drive IPs were NOT in the C# source (runtime-configured XML) —
**TODO(Casey): set the real North/South drive IPs** (placeholders
below, editable on the connection bar / Settings).

``ramp_hz_per_s`` is the host-side setpoint ramp. It deliberately
REPLACES the C#'s crude protection (``setMotorCntrlrVelocity`` lines
158–172: any requested change >2 ft/s from the current speed →
command reference 0, i.e. slam the fan toward zero!). The Python
driver instead ramps the commanded reference smoothly toward the
setpoint and never step-jumps the fan.

``reference_sign = -1`` preserves the C# direction convention
(``writeSingleRegister(slave, 2, (short)-rpmScaledTo20000)``, line
191) — these fans are commanded with a NEGATIVE reference.
**VERIFY on first live run at a tiny reference** — a wrong sign would
command the fan in REVERSE.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

TUNNELS = ("north", "south")

# per-tunnel defaults: (label, drive IP).
# TODO(Casey): set the North/South drive IPs — 192.168.0.1 is a
# PLACEHOLDER (the C# read the real IPs from a runtime XML that is not
# in the source tree). Edit on the connection bar, then Set as Defaults.
_TUNNEL_DEFAULTS = {
    "north": ("North LSWT", "192.168.0.1"),
    "south": ("South LSWT", "192.168.0.1"),
}


def defaults_path(tunnel: str = "north") -> Path:
    """Where "Set as Defaults" persists the startup config, per tunnel.

    ``defaults_north.json`` / ``defaults_south.json`` inside
    ``~/.lswt/``, or inside the directory named by the
    ``LSWT_DEFAULTS`` env var (tests). Auto-loaded at app launch.
    """
    tunnel = _check_tunnel(tunnel)
    env = os.environ.get("LSWT_DEFAULTS")
    base = Path(env) if env else (Path.home() / ".lswt")
    return base / f"defaults_{tunnel}.json"


def _check_tunnel(tunnel: str) -> str:
    t = str(tunnel).lower()
    if t not in TUNNELS:
        raise ValueError(f"tunnel must be one of {TUNNELS}, got {tunnel!r}")
    return t


@dataclass
class LswtConfig:
    """All user-tunable settings for one LSWT fan drive."""

    tunnel: str = "north"            # "north" | "south"
    label: str = "North LSWT"
    ip: str = "192.168.0.1"          # PLACEHOLDER — TODO(Casey): real IPs
    port: int = 502
    unit_id: int = 1
    modbus_timeout_s: float = 2.0

    # ── control ──────────────────────────────────────────────────────────
    max_hz: float = 60.0             # reference clamp (drive full speed)
    # Host-side setpoint ramp (Hz/s). Replaces the C#'s ">2 fps change →
    # command 0" lockout (see module docstring) — the commanded
    # reference always ramps smoothly, never step-jumps.
    ramp_hz_per_s: float = 2.0
    # The C# wrote the NEGATIVE of the scaled reference — a direction
    # convention on these fans. VERIFY on first live run at a tiny
    # reference: a wrong sign would command REVERSE.
    reference_sign: int = -1

    # ── monitor ──────────────────────────────────────────────────────────
    poll_s: float = 0.25             # actual-Hz poll / ramp tick period
    stale_after_s: float = 3.0       # no successful poll → status STALE

    # ── behaviour / display ──────────────────────────────────────────────
    force_sim: bool = False
    plot_window_s: float = 120.0
    sim_tau_s: float = 3.0           # sim fan first-order time constant

    def __post_init__(self):
        self.tunnel = _check_tunnel(self.tunnel)
        if self.reference_sign not in (-1, 1):
            raise ValueError("reference_sign must be -1 or +1")

    @classmethod
    def for_tunnel(cls, tunnel: str, **kw) -> "LswtConfig":
        """Factory defaults for one tunnel (label + IP placeholder)."""
        tunnel = _check_tunnel(tunnel)
        label, ip = _TUNNEL_DEFAULTS[tunnel]
        kw.setdefault("label", label)
        kw.setdefault("ip", ip)
        return cls(tunnel=tunnel, **kw)

    # ── serialization ────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "LswtConfig":
        """Build from a dict, ignoring unknown keys (forward compat)."""
        known = {f for f in cls.__dataclass_fields__}      # noqa: E1101
        return cls(**{k: v for k, v in d.items() if k in known})

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path) -> "LswtConfig":
        return cls.from_dict(json.loads(Path(path).read_text(
            encoding="utf-8")))


def load_startup_config(tunnel: str = "north") -> "LswtConfig":
    """App-launch auto-load: the tunnel's ``defaults_path()`` if present.

    Guarded — an unreadable/corrupt defaults file logs a warning and
    falls back to the tunnel's factory defaults.
    """
    import logging
    tunnel = _check_tunnel(tunnel)
    p = defaults_path(tunnel)
    if p.exists():
        try:
            cfg = LswtConfig.load(p)
            logging.getLogger(__name__).info("defaults loaded from %s", p)
            return cfg
        except Exception as exc:                       # noqa: BLE001
            logging.getLogger(__name__).warning(
                "defaults file %s unreadable (%s) — factory defaults",
                p, exc)
    return LswtConfig.for_tunnel(tunnel)
