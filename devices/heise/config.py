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
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional, Union

BAUD_RATES = (300, 600, 1200, 2400, 4800, 9600)


def defaults_path() -> Path:
    """Where the startup defaults persist (house pattern).

    Same convention as traverse_swt/lswt: overridable via the
    ``HEISE_DEFAULTS`` env var (tests); default
    ``~/.heise/defaults.json``. Loaded by :func:`load_startup_config` —
    hosts (Freestream) use it so an embedded session starts from the
    same config the operator proved out on the rig (the working COM
    port in particular), instead of factory placeholders.
    """
    env = os.environ.get("HEISE_DEFAULTS")
    return Path(env) if env else Path.home() / ".heise" / "defaults.json"

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
    """Pressure unit name or code → EUNIT code.

    Any non-negative INT is passed through verbatim — the instrument
    validates the code, and firmware may expose engineering units
    beyond the documented 0-12 (live 2026-07-23: a rig indicator was
    set to code 16). ``unit_name`` renders an unknown code as
    ``"code16"``; this MUST round-trip back to 16 so a saved/echoed
    config unit can be re-applied without an ``Unknown pressure unit``
    crash (the freestream device_configs bundle stores the string).
    """
    if isinstance(unit, int):
        if unit < 0:
            raise ValueError(f"Unknown EUNIT code {unit}")
        return unit
    s = unit.strip()
    if s.lower() in _UNIT_CODES:
        return _UNIT_CODES[s.lower()]
    if s.lower().startswith("code"):            # "code16" → 16 (round-trip)
        try:
            return int(s[4:])
        except ValueError:
            pass
    raise ValueError(
        f"Unknown pressure unit {unit!r} — one of "
        f"{sorted(_UNIT_CODES)} (or an EUNIT code the instrument "
        f"accepts, e.g. 'code16')")


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

    # Default port layout (CORRECTED 2026-07-24 from Casey's HIL check —
    # pressure and temperature were previously swapped, so the reduction read
    # temperature as pressure and vice versa): the instrument transmits
    # ``?`` values in port order PRESSURE first, TEMPERATURE second, so the
    # LEFT/first port is the ABSOLUTE PRESSURE sensor (psi) and the RIGHT/
    # second port is the RTD TEMPERATURE (deg F). Both ports are fully
    # reconfigurable.
    left: HeisePortConfig = field(default_factory=lambda: HeisePortConfig(
        name="Pressure", role="pressure", unit="psi"))
    right: HeisePortConfig = field(default_factory=lambda: HeisePortConfig(
        name="Temperature", role="temperature", unit="F"))

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
            name="Pressure", role="pressure", unit="psi"))
        right = mk_port(d.pop("right", None), HeisePortConfig(
            name="Temperature", role="temperature", unit="F"))
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


def load_startup_config() -> "HeiseConfig":
    """Startup auto-load: ``defaults_path()`` if present.

    Guarded — an unreadable/corrupt defaults file logs a warning and
    falls back to factory defaults.
    """
    import logging
    p = defaults_path()
    if p.exists():
        try:
            cfg = HeiseConfig.load(p)
            logging.getLogger(__name__).info("defaults loaded from %s", p)
            return cfg
        except Exception as exc:                       # noqa: BLE001
            logging.getLogger(__name__).warning(
                "defaults file %s unreadable (%s) — factory defaults",
                p, exc)
    return HeiseConfig()
