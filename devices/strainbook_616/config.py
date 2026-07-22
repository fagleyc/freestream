"""Runtime configuration for the StrainBook/616 interface.

Defaults reproduce the rig's standard LabVIEW "Force Balance" setup
(``channel setups.png``): the internal balance bridge channels on CH1–6
with the Streamlined names, plus the excitation readback on CH8.

| CH | Name       | Bridge | Exc  | Filter | Range    |
|----|------------|--------|------|--------|----------|
| 1  | N1         | Full   | 10 V | 1 kHz  | ±11 mV   |
| 2  | N2         | Full   | 10 V | 1 kHz  | ±11 mV   |
| 3  | Y1         | Full   | 10 V | 1 kHz  | ±11 mV   |
| 4  | Y2         | Full   | 10 V | 1 kHz  | ±11 mV   |
| 5  | Axial      | Full   | 10 V | 1 kHz  | ±32 mV   |
| 6  | Roll       | Full   | 10 V | 1 kHz  | ±32 mV   |
| 8  | Excitation | —      | —    | 1 kHz  | exc volts (OutSource) |

Bridge channels display **input-referred volts** (scale forced to 1.0);
recorded data is always raw volts. Converting to forces is
Streamlined/AeroVIS's job via the balance ``.vol`` calibration.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List

from . import daqx


# ── balance layout → bridge channel NAMES ────────────────────────────────
# The four bridge channels (front-panel CH1-4) are physically identical for
# both balance layouts — only their NAMES differ. Streamlined's reducer keys
# on these exact names (verified in utils/windtunnel/transforms.py::
# calc_brf_forces): a Force balance uses N1/N2/Y1/Y2, a Moment balance uses
# AftPitch/AftYaw/FwdPitch/FwdYaw. Axial, Roll and Excitation are identical
# in both. Switching balance_config is a pure RENAME of these four channels.
BRIDGE_NAMES = {
    "Force": ("N1", "N2", "Y1", "Y2"),
    "Moment": ("AftPitch", "AftYaw", "FwdPitch", "FwdYaw"),
}
#: any known bridge name → its position (0-3), so a rename can map across
#: the two name sets regardless of the current layout
_BRIDGE_INDEX = {name: i
                 for names in BRIDGE_NAMES.values()
                 for i, name in enumerate(names)}


@dataclass
class StrainChannelConfig:
    """One StrainBook channel."""
    channel: int = 1            # 1..8 (built-in unit)
    name: str = ""
    enabled: bool = True
    bridge: int = daqx.BRIDGE_FULL
    range_mv: float = 11.0      # requested ± input range; snaps to IAG×PGA
    filter_type: int = daqx.FILTER_1KHZ
    ac_couple: bool = False
    invert: bool = False
    # SSH gave dead (never-sampled) channels in live bring-up; default off
    # until the SSH scan-slot behaviour is fully understood. The 1 kHz
    # filter + 200 Hz scan keeps inter-channel skew negligible meanwhile.
    ssh: bool = False
    read_excitation: bool = False   # OutSource = excitation volts readback
    # ``scale`` is a software DISPLAY gain only (eng units per volt); recorded
    # data is always RAW volts. Forced to 1.0 everywhere — the live display is
    # therefore in volts. Kept as a field (default 1.0) so ``volts_to_eng`` and
    # old JSON configs stay valid; it is never exposed in the UI.
    scale: float = 1.0
    offset: float = 0.0
    unit: str = "V"

    @property
    def gain(self) -> tuple:
        """(total_gain, iag_code, pga_code) for the requested range."""
        if self.read_excitation:
            return (1.0, 0, 0)      # excitation readback at ×1
        return daqx.pick_gain_for_range(self.range_mv)

    def volts_to_eng(self, volts):
        return volts * self.scale + self.offset


def default_channels(balance_config: str = "Force"
                     ) -> List[StrainChannelConfig]:
    bridge = BRIDGE_NAMES.get(balance_config, BRIDGE_NAMES["Force"])
    chans = []
    for ch, name in zip((1, 2, 3, 4), bridge):
        chans.append(StrainChannelConfig(channel=ch, name=name,
                                         range_mv=11.0, scale=1.0, unit="V"))
    for ch, name in ((5, "Axial"), (6, "Roll")):
        chans.append(StrainChannelConfig(channel=ch, name=name,
                                         range_mv=32.0, scale=1.0, unit="V"))
    chans.append(StrainChannelConfig(
        channel=8, name="Excitation", read_excitation=True, ssh=False,
        bridge=daqx.BRIDGE_FULL, range_mv=5000.0, scale=1.0,
        offset=0.0, unit="V"))
    # CH8 reads the EXTERNAL excitation supply back (OutSource = exc volts,
    # ×1, full-bridge completion). Offset defaults to 0 → the channel
    # reports the excitation-readback voltage verbatim, no software shift.
    return chans


@dataclass
class StrainbookConfig:
    """All user-tunable settings for one StrainBook session."""

    # ── Device ───────────────────────────────────────────────────────────
    device_name: str = "StrainBook_0"   # alias from the Daq applet
    device_ip: str = "192.168.1.123"    # informational
    dll_path: str = ""

    # ── Excitation ──────────────────────────────────────────────────────
    # The rig powers the bridges from an EXTERNAL supply, which is the sole
    # authority. The driver NEVER commands the StrainBook's internal
    # excitation DAC/banks — CH8 only READS the external excitation back
    # (the "0 to 10 V" range).

    # ── Acquisition ──────────────────────────────────────────────────────
    scan_hz: float = 200.0
    buffer_seconds: float = 5.0
    poll_ms: int = 25

    # ── Balance calibration (.vol) ───────────────────────────────────────
    vol_path: str = ""                  # auto-loaded at startup when set
    cal_type: str = "Linear"            # Linear | Quadratic | Cubic
    balance_config: str = "Force"       # Force | Moment (balance layout)
    warn_utilization: float = 0.8       # amber above this fraction of max
    # balance metadata (filled from the .vol; saved with configs/datasets)
    balance_type: str = ""
    balance_serial: str = ""

    # ── Behaviour / display ──────────────────────────────────────────────
    force_sim: bool = False
    plot_window_s: float = 30.0
    tile_avg_ms: int = 200

    channels: List[StrainChannelConfig] = field(
        default_factory=default_channels)

    def enabled_channels(self) -> List[StrainChannelConfig]:
        return [c for c in self.channels if c.enabled and c.name.strip()]

    # ── balance layout ───────────────────────────────────────────────────
    def set_balance_config(self, balance_config: str) -> Dict[str, str]:
        """Switch the balance layout, RENAMING the four bridge channels.

        Force → N1/N2/Y1/Y2, Moment → AftPitch/AftYaw/FwdPitch/FwdYaw. Any
        channel currently carrying a known bridge name is remapped to the
        target layout's name at the same bridge position; Axial, Roll and
        Excitation are untouched (identical in both layouts). Returns the
        ``{old_name: new_name}`` map actually applied so the caller can keep
        a running device's ring-buffer / tare keys in sync.
        """
        if balance_config not in BRIDGE_NAMES:
            raise ValueError(
                f"balance_config must be one of {sorted(BRIDGE_NAMES)}, "
                f"got {balance_config!r}")
        target = BRIDGE_NAMES[balance_config]
        renames: Dict[str, str] = {}
        for ch in self.channels:
            idx = _BRIDGE_INDEX.get(ch.name)
            if idx is None:
                continue                       # Axial/Roll/Excitation/custom
            new = target[idx]
            if new != ch.name:
                renames[ch.name] = new
                ch.name = new
        self.balance_config = balance_config
        return renames

    # ── serialization ────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "StrainbookConfig":
        d = dict(d)
        chans = d.pop("channels", None)
        known = {f for f in cls.__dataclass_fields__}      # noqa: E1101
        cfg = cls(**{k: v for k, v in d.items()
                     if k in known and k != "channels"})
        if chans is not None:
            ck = {f for f in
                  StrainChannelConfig.__dataclass_fields__}  # noqa: E1101
            cfg.channels = [
                StrainChannelConfig(**{k: v for k, v in c.items() if k in ck})
                for c in chans
            ]
        return cfg

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path) -> "StrainbookConfig":
        return cls.from_dict(json.loads(Path(path).read_text(
            encoding="utf-8")))
