"""Runtime configuration for the NI USB-6351 (X-series) interface.

Defaults reproduce the rig's "Force Balance" wiring on the NI box: the six
balance bridge outputs (via external signal conditioning) on AI0–AI5 with
the Streamlined names, the excitation readback on AI6, and a spare tunnel
channel on AI7 (disabled until wired).

| AI | Name       | Terminal | Range   | Balance |
|----|------------|----------|---------|---------|
| 0  | N1         | DIFF     | ±0.2 V  | yes     |
| 1  | N2         | DIFF     | ±0.2 V  | yes     |
| 2  | Y1         | DIFF     | ±0.2 V  | yes     |
| 3  | Y2         | DIFF     | ±0.2 V  | yes     |
| 4  | Axial      | DIFF     | ±0.5 V  | yes     |
| 5  | Roll       | DIFF     | ±0.5 V  | yes     |
| 6  | Excitation | DIFF     | ±10 V   | readback|
| 7  | Spare      | DIFF     | ±10 V   | no      |

Bridge channels display raw volts (scale forced to 1.0); recorded data is
always raw volts. Converting to forces is Streamlined/AeroVIS's job via the
balance ``.vol`` calibration. Extra (non-balance) channels may carry real
EU slopes (``scale``/``offset``/``unit``) like the DaqBook tunnel channels.

The USB-6351 also provides 2 analog outputs (AO0/AO1, ±10 V) and hardware
start-trigger routing (PFI0–15 digital edge, APFI0/AI analog edge) — both
configured here and applied by the driver at connect.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List

# ── native AI input ranges (volts, ±) — nearest covering range is used ───
AI_RANGES_V = (0.1, 0.2, 0.5, 1.0, 2.0, 5.0, 10.0)
#: valid AI terminal configurations for the X-series front end
TERMINALS = ("DIFF", "RSE", "NRSE")
#: hardware trigger modes for the AI sample clock start trigger
TRIGGER_MODES = ("immediate", "digital_edge", "analog_edge")
#: digital trigger sources (PFI lines routed on the 6351 I/O connector)
PFI_SOURCES = tuple(f"PFI{i}" for i in range(16))
#: analog trigger sources (dedicated APFI0 pin or a scanned AI channel)
ANALOG_SOURCES = ("APFI0",) + tuple(f"ai{i}" for i in range(16))
#: AO physical channels on the USB-6351
AO_CHANNELS = (0, 1)
#: AO waveform shapes (``none`` = static DC level)
AO_WAVEFORMS = ("none", "sine", "square", "triangle")


# ── balance layout → bridge channel NAMES ────────────────────────────────
# Same rename machinery as strainbook_616: the four bridge channels are
# physically identical for both balance layouts — only their NAMES differ
# (Streamlined's reducer keys on the exact names). Switching balance_config
# is a pure RENAME of these four channels.
BRIDGE_NAMES = {
    "Force": ("N1", "N2", "Y1", "Y2"),
    "Moment": ("AftPitch", "AftYaw", "FwdPitch", "FwdYaw"),
}
_BRIDGE_INDEX = {name: i
                 for names in BRIDGE_NAMES.values()
                 for i, name in enumerate(names)}


def pick_range(v_min: float, v_max: float) -> float:
    """Smallest native ± range covering the requested span."""
    need = max(abs(v_min), abs(v_max))
    for r in AI_RANGES_V:
        if r >= need - 1e-9:
            return r
    return AI_RANGES_V[-1]


@dataclass
class ChannelConfig:
    """One analog input channel."""
    channel: int = 0            # ai0..ai15
    name: str = ""
    enabled: bool = True
    terminal: str = "DIFF"      # DIFF | RSE | NRSE
    v_min: float = -10.0        # requested range; nearest native ± is used
    v_max: float = 10.0
    balance: bool = False       # bridge channel: tared + fed to balcal
    # ``scale`` on balance channels is a display gain only and forced to
    # 1.0 (recorded data is always raw volts); extra channels may carry
    # real transducer slopes like the DaqBook tunnel channels.
    scale: float = 1.0
    offset: float = 0.0
    unit: str = "V"

    @property
    def native_range(self) -> float:
        """Native ± input range (volts) covering the requested span."""
        return pick_range(self.v_min, self.v_max)

    @property
    def physical(self) -> str:
        return f"ai{self.channel}"

    def volts_to_eng(self, volts):
        return volts * self.scale + self.offset


@dataclass
class AOChannelConfig:
    """One analog output channel (static level or regenerated waveform)."""
    channel: int = 0            # ao0 | ao1
    name: str = ""
    enabled: bool = False
    v_min: float = -10.0
    v_max: float = 10.0
    static_v: float = 0.0       # commanded DC level (waveform "none")
    waveform: str = "none"      # none | sine | square | triangle
    amplitude_v: float = 1.0    # waveform peak amplitude about offset_v
    freq_hz: float = 10.0
    offset_v: float = 0.0

    @property
    def physical(self) -> str:
        return f"ao{self.channel}"

    def clamp(self, volts: float) -> float:
        return min(max(volts, self.v_min), self.v_max)


@dataclass
class TriggerConfig:
    """AI start-trigger setup, applied to the task at connect.

    ``immediate`` starts sampling on start(); ``digital_edge`` arms and
    waits for an edge on a PFI line; ``analog_edge`` arms and waits for the
    source to cross ``level_v``. Continuous acquisition supports a start
    trigger only (no reference/pre-trigger).
    """
    mode: str = "immediate"     # immediate | digital_edge | analog_edge
    source: str = "PFI0"        # PFI line, or APFI0 / aiN for analog_edge
    edge: str = "rising"        # rising | falling
    level_v: float = 1.0        # analog_edge threshold


def default_channels(balance_config: str = "Force") -> List[ChannelConfig]:
    bridge = BRIDGE_NAMES.get(balance_config, BRIDGE_NAMES["Force"])
    chans = []
    for ai, name in zip((0, 1, 2, 3), bridge):
        chans.append(ChannelConfig(channel=ai, name=name, balance=True,
                                   v_min=-0.2, v_max=0.2))
    for ai, name in ((4, "Axial"), (5, "Roll")):
        chans.append(ChannelConfig(channel=ai, name=name, balance=True,
                                   v_min=-0.5, v_max=0.5))
    chans.append(ChannelConfig(channel=6, name="Excitation",
                               v_min=-10.0, v_max=10.0))
    chans.append(ChannelConfig(channel=7, name="Spare", enabled=False,
                               v_min=-10.0, v_max=10.0))
    return chans


def default_ao_channels() -> List[AOChannelConfig]:
    return [AOChannelConfig(channel=0, name="AO0"),
            AOChannelConfig(channel=1, name="AO1")]


@dataclass
class NiDaqConfig:
    """All user-tunable settings for one NI USB-6351 session."""

    # ── Device ───────────────────────────────────────────────────────────
    device_name: str = "Dev2"          # NI-MAX device alias

    # ── Acquisition ──────────────────────────────────────────────────────
    scan_hz: float = 1000.0            # per-channel sample rate
    buffer_seconds: float = 5.0        # DAQmx input buffer length
    poll_ms: int = 25                  # poll-thread read period

    # ── Trigger ──────────────────────────────────────────────────────────
    trigger: TriggerConfig = field(default_factory=TriggerConfig)

    # ── Analog output ────────────────────────────────────────────────────
    ao_channels: List[AOChannelConfig] = field(
        default_factory=default_ao_channels)
    ao_update_hz: float = 10_000.0     # waveform sample clock

    # ── Balance calibration (.vol) ───────────────────────────────────────
    vol_path: str = ""                 # auto-loaded at startup when set
    cal_type: str = "Linear"           # Linear | Quadratic | Cubic
    balance_config: str = "Force"      # Force | Moment (balance layout)
    warn_utilization: float = 0.8      # amber above this fraction of max
    balance_type: str = ""
    balance_serial: str = ""

    # ── Behaviour / display ──────────────────────────────────────────────
    force_sim: bool = False
    plot_window_s: float = 30.0
    tile_avg_ms: int = 200

    channels: List[ChannelConfig] = field(default_factory=default_channels)

    def enabled_channels(self) -> List[ChannelConfig]:
        return [c for c in self.channels if c.enabled and c.name.strip()]

    def enabled_ao_channels(self) -> List[AOChannelConfig]:
        return [c for c in self.ao_channels if c.enabled and c.name.strip()]

    # ── balance layout ───────────────────────────────────────────────────
    def set_balance_config(self, balance_config: str) -> Dict[str, str]:
        """Switch the balance layout, RENAMING the four bridge channels.

        Force → N1/N2/Y1/Y2, Moment → AftPitch/AftYaw/FwdPitch/FwdYaw. Any
        channel currently carrying a known bridge name is remapped to the
        target layout's name at the same bridge position. Returns the
        ``{old_name: new_name}`` map actually applied so the caller can
        keep a running device's ring-buffer / tare keys in sync.
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
                continue                       # Axial/Roll/extra channels
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
    def from_dict(cls, d: dict) -> "NiDaqConfig":
        d = dict(d)
        chans = d.pop("channels", None)
        ao = d.pop("ao_channels", None)
        trig = d.pop("trigger", None)
        known = {f for f in cls.__dataclass_fields__}      # noqa: E1101
        cfg = cls(**{k: v for k, v in d.items() if k in known})
        if chans is not None:
            ck = {f for f in ChannelConfig.__dataclass_fields__}  # noqa
            cfg.channels = [
                ChannelConfig(**{k: v for k, v in c.items() if k in ck})
                for c in chans
            ]
        if ao is not None:
            ak = {f for f in AOChannelConfig.__dataclass_fields__}  # noqa
            cfg.ao_channels = [
                AOChannelConfig(**{k: v for k, v in c.items() if k in ak})
                for c in ao
            ]
        if trig is not None:
            tk = {f for f in TriggerConfig.__dataclass_fields__}  # noqa
            cfg.trigger = TriggerConfig(
                **{k: v for k, v in trig.items() if k in tk})
        return cfg

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path) -> "NiDaqConfig":
        return cls.from_dict(json.loads(Path(path).read_text(
            encoding="utf-8")))
