"""IDeal wire protocol for IDC SmartStep23 SmartDrives (South LSWT traverse).

Three SmartStep23 microstepping drives on ONE RS-232C daisy chain
(9600 8N1, XON/XOFF, 3-wire), unit-addressed 1 = Z, 2 = Y, 3 = X.
Protocol source: ``SmartStep23_User's_Manual.pdf`` Chapter 8 (IDC /
Danaher Motion, P/N PCW-5008).

Wire format
-----------
* Commands are two UPPERCASE ASCII letters, optionally prefixed by the
  unit address (``2PA1``); an unaddressed command goes to ALL units.
  Delimiters are ``<cr>`` or space.
* Responses are prefixed ``*`` and end at ``<cr>`` (``*+1.000<cr>``) —
  the asterisk tells the other drives on the chain to ignore the
  response characters.
* RS-232C ECHO must be ON for the daisy chain to pass characters
  through, so the host reads back its OWN command bytes before any
  response. The parser here treats everything before the ``*`` as echo
  noise and discards it.

Command inventory used by this driver (all manual-verified):

===========  ==========================================================
``<n>PA1``   current position, user units → ``*+1.000``
``<n>SA1``   axis status, 4-digit hex → ``*002A`` (bits below)
``<n>SD1``   drive status, 4-digit hex (enabled / faults)
``<n>SS``    system status, 4-digit hex (ready / axis fault)
``<n>MN``    model number → ``*SmartStep23``
``<n>K``     kill — instant halt, NO decel ramp (panic only)
``<n>S``     stop — decelerate to a halt (address optional)
``<n>CB``    clear the command buffer
``<n>EAi``   enable amplifier (1) / disable (0)
``<n>ACr``   acceleration            (buffered)
``<n>DEr``   deceleration            (buffered)
``<n>VEr``   velocity — SIGN sets direction inside an MC move
``<n>DAr``   distance absolute       (buffered; needs GO)
``<n>DIr``   distance incremental    (buffered; needs GO)
``<n>MC+``   arm a continuous move (jog); VE±r GO runs it, VE0 GO or
             S ends it
``<n>GO``    start the buffered move
``<n>SPr``   SET POSITION — the whole homing story here: jog to the
             reference spot, send SP0, done
===========  ==========================================================
"""

from __future__ import annotations

import re
from typing import Optional

# ── serial line parameters (manual: Comm Port Settings, ch. 8-19) ──────
BAUD = 9600
DATA_BITS = 8
PARITY = "N"
STOP_BITS = 1
XONXOFF = True

#: command / response delimiter
CR = b"\r"

#: unit addresses on the chain, exactly as labelled on the rig
#: (drive 1 = Z vertical, drive 2 = Y lateral, drive 3 = X axial)
UNIT_Z = 1
UNIT_Y = 2
UNIT_X = 3

# ── SA (axis status) bits — manual page 8-37, bit numbers 1-based ──────
SA_MOVING = 0x0001            # bit 1: steps being sent to the amplifier
SA_AT_VELOCITY = 0x0002       # bit 2: stepping at a constant rate
SA_MOVE_COMPLETE = 0x0008     # bit 4: last move finished cleanly
SA_HOME_OK = 0x0010           # bit 5: last GH homing succeeded (unused)
SA_HOME_SWITCH = 0x0020       # bit 6: home switch hardware state
SA_LIMIT_NEG = 0x0040         # bit 7: − limit switch (NC) engaged
SA_LIMIT_POS = 0x0080         # bit 8: + limit switch (NC) engaged
SA_LIMIT_NEG_LATCH = 0x0100   # bit 9: move terminated by − limit
SA_LIMIT_POS_LATCH = 0x0200   # bit 10: move terminated by + limit

# ── SD (drive status) bits — manual page 8-38 ──────────────────────────
SD_FOLLOWING_ERROR = 0x0001
SD_OVER_CURRENT = 0x0002
SD_THERMAL_FAULT = 0x0004
SD_ENABLED = 0x0010           # bit 5: amplifier enabled
SD_AMP_FAULT = 0x0100         # bit 9: amp faulted (power cycle to clear)
SD_FAULT_MASK = (SD_FOLLOWING_ERROR | SD_OVER_CURRENT
                 | SD_THERMAL_FAULT | SD_AMP_FAULT)

# ── SS (system status) bits — manual page 8-39 ─────────────────────────
SS_READY = 0x0001             # bit 1: ready to buffer RS-232C commands
SS_FLASH_ERROR = 0x0002
SS_AXIS1_FAULT = 0x0100       # bit 9: amp fault / following error / limit


class ProtocolError(RuntimeError):
    """A malformed or missing response from the chain."""


def command(unit: Optional[int], mnemonic: str, arg: str = "") -> bytes:
    """One wire command: optional unit address + mnemonic + argument + CR.

    ``unit=None`` broadcasts (no address) — only meaningful for S / K.
    """
    prefix = "" if unit is None else str(int(unit))
    return f"{prefix}{mnemonic}{arg}".encode("ascii") + CR


_RESPONSE_RE = re.compile(rb"\*([^\r\n]*)")


def extract_response(buf: bytes) -> Optional[str]:
    """Pull the first complete ``*...<cr>`` response out of a byte
    stream, ignoring the echoed command characters around it.

    Returns the payload (without the asterisk) or None while the
    response is still incomplete. The chain's mandatory echo means every
    read starts with our own transmitted bytes — everything before the
    ``*`` is discarded.
    """
    star = buf.find(b"*")
    if star < 0:
        return None
    end = buf.find(CR, star)
    if end < 0:
        end = buf.find(b"\n", star)
    if end < 0:
        return None
    return buf[star + 1:end].decode("ascii", errors="replace").strip()


def parse_position(payload: str) -> float:
    """``+1.000`` / ``-0.5000`` → float (user units, inches here)."""
    try:
        return float(payload)
    except ValueError as exc:
        raise ProtocolError(f"bad position response {payload!r}") from exc


def parse_status_hex(payload: str) -> int:
    """``002A`` → 0x2A. SA/SD/SS all answer 4-digit hex."""
    try:
        return int(payload, 16)
    except ValueError as exc:
        raise ProtocolError(f"bad status response {payload!r}") from exc


def format_real(value: float) -> str:
    """Reals with up to 4 decimals, the manual's stated precision."""
    return f"{value:.4f}".rstrip("0").rstrip(".")
