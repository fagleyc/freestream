"""Runtime configuration for the SSWT tunnel (Red Lion G315 gateway).

We talk ONLY to the Red Lion's Modbus TCP slave at **192.168.1.50:502,
unit 1** — never directly to the VersaMax (SRTP) or the FanDrive
(RS-485 SNP, single-master: the HMI owns that line).

Write safety knobs live here on purpose:

* ``rpm_max`` **defaults to 0 = not configured** — TunnelControl refuses
  every RPM command until a real limit is set (Settings or JSON).
* ``word_order`` must be verified against a live nonzero value before
  writes are trusted; ``word_order_verified`` records that this was
  done (the GUI warns until it is).
* ``momentary_verified`` records that the 250 ms button pulse was
  checked against the physical HMI button behavior (TODO: requires a
  supervised test — see README).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, List, Tuple

WORD_ORDERS = ("low_first", "high_first")


@dataclass
class TunnelConfig:
    """All user-tunable settings for the tunnel gateway interface."""

    ip: str = "192.168.1.50"
    port: int = 502
    unit_id: int = 1

    # ── monitor ──────────────────────────────────────────────────────────
    poll_s: float = 0.5              # Block1 poll period (2 Hz)
    stale_after_s: float = 3.0       # snapshot older than this = stale
    backoff_min_s: float = 1.0       # reconnect backoff (doubles per fail)
    backoff_max_s: float = 30.0

    # ── protocol ─────────────────────────────────────────────────────────
    modbus_timeout_s: float = 2.0
    # 32-bit register pair order within a Crimson L4 element.
    # VERIFIED LIVE 2026-07-07 (probe_tunnel.py: RPM_Set=600 and three
    # lit booleans all carried their value in the LOW word).
    word_order: str = "low_first"
    word_order_verified: bool = True
    # Crimson stores fixed-point tags ×10^decimals; the RPM tags have 1
    # display decimal, so the register is RPM×10. VERIFIED LIVE
    # 2026-07-07: register 600 while the Red Lion screen shows 60.0.
    rpm_scale: float = 0.1


    # ── bearing temperatures (opt-in extended Block1 read) ──────────────
    # The analog bearing sensors (VersaMax AI0007–AI0009 =
    # Analog_Feedback.B1/B2/B3) are NOT in the shipped Crimson gateway
    # database — extend the read block with elements 17–19 in Crimson
    # FIRST (see README), then flip this on. When True the Block1 poll
    # becomes one contiguous 38-register read and the snapshot carries
    # bearing_b1/b2/b3 as scaled floats.
    bearing_temps: bool = False
    # Per-channel linear scaling [raw_lo, raw_hi, eng_lo, eng_hi], from
    # the Crimson tunnel_tags.csv export.
    bearing_cal_b1: List[float] = field(
        default_factory=lambda: [955.0, 5035.0, 0.0, 150.0])
    bearing_cal_b2: List[float] = field(
        default_factory=lambda: [969.0, 4979.0, 0.0, 150.0])
    bearing_cal_b3: List[float] = field(
        default_factory=lambda: [930.0, 4994.0, 0.0, 150.0])
    # Display unit for the scaled values. NOTE: the CSV scaling gives a
    # 0–150 span that LOOKS like °C, but the cal vintage/unit has not
    # been confirmed on the rig — verify against the bearing RTD spec
    # (could be °F) before trusting absolute values.
    bearing_unit: str = "°C"

    # ── writes (TunnelControl) ───────────────────────────────────────────
    rpm_max: float = 0.0             # 0 = NOT CONFIGURED → RPM writes refuse
    button_hold_ms: int = 250        # momentary pulse width
    momentary_verified: bool = False  # TODO: physical test vs HMI buttons

    # ── behaviour / display ──────────────────────────────────────────────
    force_sim: bool = False
    plot_window_s: float = 300.0

    def __post_init__(self):
        if self.word_order not in WORD_ORDERS:
            raise ValueError(f"word_order must be one of {WORD_ORDERS}")

    def bearing_cal(self) -> Dict[str, Tuple[float, float, float, float]]:
        """Cal constants keyed by snapshot attribute (decode_block1)."""
        return {"bearing_b1": tuple(self.bearing_cal_b1),
                "bearing_b2": tuple(self.bearing_cal_b2),
                "bearing_b3": tuple(self.bearing_cal_b3)}

    # ── serialization ────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TunnelConfig":
        known = {f for f in cls.__dataclass_fields__}      # noqa: E1101
        return cls(**{k: v for k, v in d.items() if k in known})

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path) -> "TunnelConfig":
        return cls.from_dict(json.loads(Path(path).read_text(
            encoding="utf-8")))
