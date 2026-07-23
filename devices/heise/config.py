"""Configuration for the Heise PM digital indicator (RS-232 remote
protocol).

Protocol facts (manual ``manual-pm-digital-indicator-im.pdf``, §13 and
Appendix A, "Indicator serial port functions V1.0"):

* 300/600/1200/2400/4800/9600 baud, 8 data bits, 1 stop bit, no parity,
  no flow control, straight-through cable. Lines end with a selectable
  end-of-message character — configure the indicator for CRLF.
* Set the indicator's interface to the REMOTE protocol (shift →
  Output → "remote" → baud → EOM).
* ``?`` returns the current measurement(s): ``0.004469,-0.000227``
  (left,right — one value per active port).
* ``EUNIT l, r`` / ``EUNIT?`` set/get the engineering-unit code per
  port; ``ZERO l, r``; ``TARE l, r`` / ``TARE?``; ``DAMP n`` /
  ``DAMP?``; ``MINMAX?``; ``BATCK?``; ``LASTERR?``; ``PORT n`` /
  ``PORT?`` (display layout 0–5); ``HOLD``; ``KEYLOCK``.

A port carries either a pressure sensor or an RT-1/RT-2 RTD module
(temperature in F/C/K/Rankine/ohms — the temperature unit is selected
on the instrument; pressure units are remotely selectable by code).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Union

BAUD_RATES = (300, 600, 1200, 2400, 4800, 9600)

#: EUNIT codes from Appendix A (drawing 822B122 conversion factors).
#: Code 1 is the indicator's base inches-of-water; code 3 is the
#: 20 °C-referenced variant.
PRESSURE_UNITS = {
    0: "psi",
    1: "inH2O",
    2: "inHg",
    3: "inH2O@20C",
    4: "ftSW",
    5: "bar",
    6: "mbar",
    7: "kPa",
    8: "MPa",
    9: "mmHg",
    10: "cmWC",
    11: "mmWC",
    12: "kg/cm2",
}
_UNIT_CODES = {name.lower(): code for code, name in PRESSURE_UNITS.items()}

TEMPERATURE_UNITS = ("F", "C", "K", "R", "ohms")

ROLES = ("pressure", "temperature", "off")


def unit_code(unit: Union[int, str]) -> int:
    """Pressure unit name or code → EUNIT code."""
    if isinstance(unit, int):
        if unit not in PRESSURE_UNITS:
            raise ValueError(f"Unknown EUNIT code {unit}")
        return unit
    try:
        return _UNIT_CODES[unit.strip().lower()]
    except KeyError:
        raise ValueError(
            f"Unknown pressure unit {unit!r} — one of "
            f"{sorted(_UNIT_CODES)}") from None


def unit_name(code: int) -> str:
    return PRESSURE_UNITS.get(code, f"code{code}")


@dataclass
class HeisePortConfig:
    """One indicator port (left or right)."""
    name: str = "Pressure"          # channel name in blocks/ring
    role: str = "pressure"          # pressure | temperature | off
    #: pressure ports: unit name/code pushed via EUNIT at connect (and
    #: changeable live). Temperature ports: display label only — the
    #: RTD unit (F/C/K/R/ohms) is selected on the instrument.
    unit: str = "psi"

    @property
    def enabled(self) -> bool:
        return self.role != "off"


@dataclass
class HeiseConfig:
    com_port: str = ""
    baud: int = 9600
    timeout_s: float = 1.0
    poll_s: float = 0.25            # ``?`` query period
    buffer_seconds: float = 3600.0
    apply_units_on_connect: bool = True
    max_consecutive_errors: int = 5
    force_sim: bool = False

    # Default port layout matches the bench instrument (live 2026-07-23:
    # '?' returned '73.61,11.43' — RTD temperature on the LEFT port,
    # pressure on the RIGHT). Both ports are fully reconfigurable.
    left: HeisePortConfig = field(default_factory=lambda: HeisePortConfig(
        name="Temperature", role="temperature", unit="F"))
    right: HeisePortConfig = field(default_factory=lambda: HeisePortConfig(
        name="Pressure", role="pressure", unit="psi"))

    def ports(self) -> List[HeisePortConfig]:
        return [self.left, self.right]

    def enabled_ports(self) -> List[HeisePortConfig]:
        return [p for p in self.ports() if p.enabled]

    # ── serialization (house pattern) ────────────────────────────────────
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "HeiseConfig":
        d = dict(d)
        known = set(cls.__dataclass_fields__)
        port_fields = set(HeisePortConfig.__dataclass_fields__)

        def mk_port(p: Optional[dict], default: HeisePortConfig):
            if not p:
                return default
            return HeisePortConfig(**{k: v for k, v in p.items()
                                      if k in port_fields})

        left = mk_port(d.pop("left", None), HeisePortConfig(
            name="Temperature", role="temperature", unit="F"))
        right = mk_port(d.pop("right", None), HeisePortConfig(
            name="Pressure", role="pressure", unit="psi"))
        return cls(left=left, right=right,
                   **{k: v for k, v in d.items()
                      if k in known and k not in ("left", "right")})

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path) -> "HeiseConfig":
        return cls.from_dict(json.loads(
            Path(path).read_text(encoding="utf-8")))
