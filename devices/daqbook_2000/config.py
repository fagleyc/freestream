"""Runtime configuration for the DaqBook/2000 interface.

Defaults reproduce the rig's standard LabVIEW setup (see
``DBook Channels.png`` / ``strainbook_daqbook_IP.png``):

* device alias ``DaqBook2005`` at 192.168.1.125
* CH0  ``Pdiff``  differential, requested 0..+3 V   (tunnel dynamic pressure)
* CH2  ``Ptot``   differential, requested -10..+10 V (total pressure)
* CH4  ``Temp``   single-ended, requested 0..+10 V   (tunnel temperature)

Engineering-unit slopes default to the Streamlined ``PRESSLOPvxi18.PCF``
transducers used at the subsonic tunnel: [220] pDiff 0.386949 psi/V and
[690] p0Stil 1.92604 psi/V.  Temp transmitter: 1 V = 10 °C (scale 10).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional

from .daqx import pick_range


@dataclass
class ChannelConfig:
    """One analog input channel."""
    channel: int = 0
    name: str = ""
    enabled: bool = True
    differential: bool = False
    v_min: float = -10.0       # requested range; nearest native range is used
    v_max: float = 10.0
    scale: float = 1.0         # engineering units per volt
    offset: float = 0.0        # engineering units at 0 V
    unit: str = "V"

    @property
    def gain_bipolar(self) -> tuple:
        """Native (gain, bipolar) covering the requested range.

        Single-ended channels are restricted to bipolar ranges (hardware
        limitation of the 2000-series front end).
        """
        return pick_range(self.v_min, self.v_max, self.differential)

    def volts_to_eng(self, volts):
        return volts * self.scale + self.offset


def default_channels() -> List[ChannelConfig]:
    return [
        ChannelConfig(channel=0, name="Pdiff", differential=True,
                      v_min=0.0, v_max=3.0,
                      scale=0.386949, offset=0.0, unit="psid"),
        ChannelConfig(channel=2, name="Ptot", differential=True,
                      v_min=-10.0, v_max=10.0,
                      scale=1.92604, offset=0.0, unit="psia"),
        ChannelConfig(channel=4, name="Temp", differential=False,
                      v_min=0.0, v_max=10.0,
                      scale=10.0, offset=0.0, unit="degC"),   # 1 V = 10 °C
    ]


@dataclass
class DaqbookConfig:
    """All user-tunable settings for one DaqBook session."""

    # ── Device ───────────────────────────────────────────────────────────
    device_name: str = "DaqBook2005"   # alias from the Daq Configuration applet
    device_ip: str = "192.168.1.125"   # informational (applet owns the mapping)
    dll_path: str = ""                 # blank = search standard locations

    # ── Acquisition ──────────────────────────────────────────────────────
    scan_hz: float = 200.0             # scan rate; plenty for tunnel
                                       # pressures/temps and light on the GUI
    buffer_seconds: float = 5.0        # circular driver buffer length
    poll_ms: int = 25                  # transfer-status poll period

    # ── Behaviour / display ──────────────────────────────────────────────
    force_sim: bool = False
    plot_window_s: float = 30.0
    tile_avg_ms: int = 200             # live tile smoothing window

    channels: List[ChannelConfig] = field(default_factory=default_channels)

    # ── helpers ──────────────────────────────────────────────────────────
    def enabled_channels(self) -> List[ChannelConfig]:
        return [c for c in self.channels if c.enabled and c.name.strip()]

    # ── serialization ────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "DaqbookConfig":
        d = dict(d)
        chans = d.pop("channels", None)
        known = {f for f in cls.__dataclass_fields__}      # noqa: E1101
        cfg = cls(**{k: v for k, v in d.items() if k in known and
                     k != "channels"})
        if chans is not None:
            ck = {f for f in ChannelConfig.__dataclass_fields__}  # noqa: E1101
            cfg.channels = [
                ChannelConfig(**{k: v for k, v in c.items() if k in ck})
                for c in chans
            ]
        return cfg

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path) -> "DaqbookConfig":
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
