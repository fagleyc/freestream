"""Runtime configuration for the ATE balance interface.

Holds the network endpoints, the connection role, the per-channel rated load
maxima, plus JSON load/save.  A helper seeds defaults from the rig's own
``OGI.ini`` when present.  (Model reference geometry lives in the Freestream
suite — this standalone driver deals in raw wind-axis loads only.)
"""

from __future__ import annotations

import configparser
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .protocol import (DEFAULT_OGIT_PORT, DEFAULT_TMSC_PORT, DEFAULT_TMSD_PORT,
                       WIRE_AXES)


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

    def __post_init__(self) -> None:
        if self.span_config not in SPAN_CONFIGS:
            raise ValueError(
                f"span_config must be one of {SPAN_CONFIGS}, "
                f"got {self.span_config!r}")
        # tolerate partial dicts from old/hand-edited JSON: every wire axis
        # always has an entry (0.0 = no limit configured)
        for axis in WIRE_AXES:
            self.max_loads.setdefault(axis, 0.0)

    # ── Display ──────────────────────────────────────────────────────────
    plot_window_s: float = 10.0        # time-history window (s) at full rate
    bar_avg_ms: int = 50               # live bar smoothing window (ms)

    # ── Rated load maxima (per wire axis; 0.0 = no limit configured) ─────
    # Keyed by the six wire axis names; units N for Lift/Drag/Side and
    # N·m for Pitch/Yaw/Roll.  The suite streams utilization bars against
    # these; the standalone app uses them only for an overstress hint.
    max_loads: Dict[str, float] = field(
        default_factory=lambda: {a: 0.0 for a in WIRE_AXES})

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
