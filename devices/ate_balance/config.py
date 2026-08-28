"""Runtime configuration for the ATE balance interface.

Holds the network endpoints, the connection role, the per-channel rated load
maxima, plus JSON load/save.  A helper seeds defaults from the rig's own
``OGI.ini`` when present.  (Model reference geometry lives in the Freestream
suite — this standalone driver deals in raw balance-frame loads only:
Fx/Fy/Fz/Mx/My/Mz in the balance axes, X back, Y right, Z up.)
"""

from __future__ import annotations

import configparser
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .protocol import (BALANCE_AXES, DEFAULT_OGIT_PORT, DEFAULT_TMSC_PORT,
                       DEFAULT_TMSD_PORT, WIRE_TO_BALANCE)


# Connection role on the control (TMSC) channel.
#   "listen" - we are the TCP server; the OGI dials us (documented behaviour).
#   "dial"   - we actively connect out to the OGI's TMSC port (fallback).
CONNECT_LISTEN = "listen"
CONNECT_DIAL = "dial"

# Model span configuration — how the logical alpha/beta axes map onto the
# rig's two physical drives (see AteBalanceDevice.goto_alpha/goto_beta):
#   "full" - full-span model (default): alpha = INCIDENCE drive (pitch,
#            GOTO_INC_POS, −10..45°); beta = YAW drive (GOTO_YAW_POS,
#            −90..90°).
#   "half" - ½-span model on the turntable: alpha = the YAW drive (the
#            semispan model gets its angle of attack from yaw); the
#            incidence drive is UNUSED and there is no beta axis.
SPAN_FULL = "full"
SPAN_HALF = "half"
SPAN_CONFIGS = (SPAN_FULL, SPAN_HALF)

# Engineering-unit system the OGI is set to send.  The OGI's own
# Settings -> Units menu offers "Kg and Kgm, N and Nm, Lb and Lbft"
# (AID-012-10015-1) and the choice is NOT carried on the wire, so the
# client has to be told which one is selected.  Note that the pound
# setting pairs lbf with lbf*FT, not in*lbf.
LOAD_UNITS = {
    "N":  ("N", "N*m"),
    "lb": ("lbf", "lbf*ft"),
    "kg": ("kgf", "kgf*m"),
}

# Pretty forms of the same, for axis labels and readouts.
LOAD_UNIT_SYMBOLS = {
    "N":  ("N", "N·m"),
    "lb": ("lbf", "lbf·ft"),
    "kg": ("kgf", "kgf·m"),
}

# Newtons per one unit of each system (force, moment).  The rated maxima
# below are stored in N / N*m so switching the OGI's unit setting cannot
# silently invalidate them; load_limits converts on the way out.
LOAD_UNIT_TO_SI = {
    "N":  (1.0, 1.0),
    "lb": (4.4482216152605, 4.4482216152605 * 0.3048),   # lbf, lbf*ft
    "kg": (9.80665, 9.80665),                            # kgf, kgf*m
}

# Design load ranges from AID-010-10015-1 2.4, in N / N*m.  Seeded as the
# rated maxima so the utilization bars are meaningful out of the box
# instead of waiting for someone to type six numbers.  Keyed by the
# balance-frame axes (Fx back, Fy right, Fz up; Mx roll, My pitch, Mz yaw).
RATED_LOADS_N = {
    "Fx": 667.0, "Fy": 1112.0, "Fz": 1112.0,     # 150 / 250 / 250 lbf
    "Mx": 339.0, "My": 203.0, "Mz": 203.0,       # 250 / 150 / 150 lbf*ft
}


def convert_loads(value: float, from_units: str, to_units: str,
                  moment: bool = False) -> float:
    """Convert one load between two of the OGI's unit systems."""
    i = 1 if moment else 0
    return (float(value) * LOAD_UNIT_TO_SI[from_units][i]
            / LOAD_UNIT_TO_SI[to_units][i])


@dataclass
class AteConfig:
    """All user-tunable settings for one balance session."""

    # ── Network ──────────────────────────────────────────────────────────
    ogi_ip: str = "192.168.1.60"       # OGI control PC (rig static IP); use
                                       # 127.0.0.1 for the emulator/OGI_Sim
    bind_host: str = "0.0.0.0"         # local interface to bind listeners on
    tmsc_port: int = DEFAULT_TMSC_PORT  # TCP control
    tmsd_port: int = DEFAULT_TMSD_PORT  # UDP data
    ogit_port: int = DEFAULT_OGIT_PORT  # UDP trigger (OGI listens here)
    connect_mode: str = CONNECT_LISTEN

    # ── Behaviour ────────────────────────────────────────────────────────
    force_sim: bool = False            # ignore sockets, generate synthetic data
    auto_trigger: bool = True          # send TMS_CONNECT on connect()
    default_sample_seconds: int = 5

    # ── Model span configuration ─────────────────────────────────────────
    # "full" (default): alpha = incidence drive, beta = yaw drive.
    # "half": ½-span model — alpha = YAW drive, incidence unused, no beta.
    # Inherited into recorded data (root attr / meta) for post-processing.
    span_config: str = SPAN_FULL

    # ── Engineering units the OGI streams ────────────────────────────────
    # Must MATCH the OGI's Settings -> Units selection: the loads arrive
    # as bare float32s with no unit tag, so a mismatch is a silent scale
    # error downstream (N read as lb is 4.45x, lb*ft read as in*lb is
    # 12x).  Inherited into the recorded channel unit attributes, which
    # is how the reduction knows what to convert from.
    # Pounds is what the OGI is set to on this rig; change it here if the
    # OGI's Units menu is changed.
    load_units: str = "lb"

    def __post_init__(self) -> None:
        if self.span_config not in SPAN_CONFIGS:
            raise ValueError(
                f"span_config must be one of {SPAN_CONFIGS}, "
                f"got {self.span_config!r}")
        if self.load_units not in LOAD_UNITS:
            raise ValueError(
                f"load_units must be one of {tuple(LOAD_UNITS)}, "
                f"got {self.load_units!r}")
        if self.max_load_units not in LOAD_UNIT_TO_SI:
            raise ValueError(
                f"max_load_units must be one of {tuple(LOAD_UNIT_TO_SI)}, "
                f"got {self.max_load_units!r}")
        # migrate max_loads saved before the balance-frame rename: JSON from
        # older builds is keyed by the wire names (Lift/Drag/Side/...).
        for wire, bal in WIRE_TO_BALANCE.items():
            if wire in self.max_loads and bal not in self.max_loads:
                self.max_loads[bal] = self.max_loads[wire]
        for wire in WIRE_TO_BALANCE:
            self.max_loads.pop(wire, None)
        # tolerate partial dicts from old/hand-edited JSON: every balance axis
        # always has an entry.  A missing or zero entry falls back to the
        # balance's published design range rather than to "no limit" —
        # a zero limit draws no bar at all, which reads as a dead readout.
        for axis in BALANCE_AXES:
            if not self.max_loads.get(axis):
                self.max_loads[axis] = RATED_LOADS_N[axis]

    # ── Display ──────────────────────────────────────────────────────────
    plot_window_s: float = 10.0        # time-history window (s) at full rate
    bar_avg_ms: int = 50               # live bar smoothing window (ms)
    # Display-only low-pass cutoff (Hz) for the live bars and traces.
    # 0 disables. The ring buffer, the dwell averages and everything the
    # recorder writes stay RAW — this smooths the picture, not the data.
    display_lpf_hz: float = 1.0

    # ── Rated load maxima (per balance axis) ────────────────────────────
    # Keyed by the six balance axes (Fx..Mz) and held in ``max_load_units``
    # (N / N*m by default), INDEPENDENT of what the OGI happens to be
    # streaming: the utilization bars convert, so flipping the OGI from
    # newtons to pounds cannot silently shrink every bar by 4.45x.
    # Seeded from the balance's published design ranges.
    max_loads: Dict[str, float] = field(
        default_factory=lambda: dict(RATED_LOADS_N))
    max_load_units: str = "N"

    # ── Auxiliary (DAQbook) channel labels — reserved for later wiring ──
    aux_channel_labels: List[str] = field(default_factory=list)

    # ── serialization ────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AteConfig":
        known = {f for f in cls.__dataclass_fields__}      # noqa: E1101
        return cls(**{k: v for k, v in d.items() if k in known})

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    @classmethod
    def load(cls, path) -> "AteConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def from_ogi_ini(cls, ini_path, **overrides) -> "AteConfig":
        """Seed a config from the rig's ``OGI.ini`` (ports + IP if present).

        The OGI.ini ``[TMSC] IP=`` is the address the OGI dials (i.e. the TMS
        machine).  We use it only as a sensible default for ``ogi_ip`` when the
        operator has not supplied one; missing keys fall back to the documented
        defaults.
        """
        cfg = cls()
        try:
            parser = configparser.ConfigParser()
            # OGI.ini has duplicate-free simple sections; tolerate odd casing.
            parser.read(ini_path, encoding="utf-8")
            for section, attr in (("TMSC", "tmsc_port"),
                                  ("TMSD", "tmsd_port"),
                                  ("OGIT", "ogit_port")):
                if parser.has_option(section, "Port"):
                    setattr(cfg, attr, parser.getint(section, "Port"))
            for section in ("TMSC", "TMSD"):
                if parser.has_option(section, "IP"):
                    ip = parser.get(section, "IP").strip()
                    if ip and ip.upper() != "<NONE>":
                        cfg.ogi_ip = ip
                        break
        except (OSError, configparser.Error):
            pass
        for k, v in overrides.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        return cfg
