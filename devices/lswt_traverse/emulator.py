"""Byte-level emulator of the SmartStep23 daisy chain.

``SimChain`` duck-types the pyserial ``Serial`` surface the driver uses
(``write`` / ``read`` / ``in_waiting`` / ``reset_input_buffer`` /
``close``), so the transport code runs UNCHANGED in simulation — echo
included: every written byte is echoed back before any ``*`` response,
exactly as the real chain does (RS-232C echo must be ON for daisy
chaining, manual ch. 8-20).

Three simulated drives (units 1 = Z, 2 = Y, 3 = X) integrate motion in
wall-clock time at the commanded VE, honour DA / DI / GO, MC± jogs, SP
re-referencing, S / K stops and EA enable, and answer PA1 / SA1 / SD1 /
SS / MN with manual-shaped payloads.
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

from . import protocol as P


class _SimDrive:
    """One SmartStep23: position integrator + IDeal command handling."""

    def __init__(self, unit: int):
        self.unit = unit
        self.position = 0.0           # user units (inches)
        self.velocity = 1.0           # magnitude last set by VE
        self.accel = 0.2
        self.enabled = True
        self.moving = False
        self.move_complete = False
        self.target: Optional[float] = None    # DA/DI move in progress
        self.continuous = 0.0         # signed VE of an MC jog, 0 = none
        self._pending = None          # buffered DA/DI/MC awaiting GO
        self._pending_ve = 0.0        # last VE value (signed)
        self._last = time.monotonic()

    # ── physics ──────────────────────────────────────────────────────────
    def advance(self) -> None:
        now = time.monotonic()
        dt = now - self._last
        self._last = now
        if not self.enabled or dt <= 0.0:
            return
        if self.target is not None:
            step = abs(self.velocity) * dt
            delta = self.target - self.position
            if abs(delta) <= step:
                self.position = self.target
                self.target = None
                self.moving = False
                self.move_complete = True
            else:
                self.position += step if delta > 0 else -step
                self.moving = True
        elif self.continuous:
            self.position += self.continuous * dt
            self.moving = True
        else:
            self.moving = False

    # ── command handling ─────────────────────────────────────────────────
    def handle(self, mnemonic: str, arg: str) -> Optional[str]:
        """Execute one command; return a response payload or None."""
        self.advance()
        if mnemonic == "PA":                       # PA1
            return f"{self.position:+.4f}"
        if mnemonic == "SA":                       # SA1
            status = 0
            if self.moving:
                status |= P.SA_MOVING
            else:
                status |= P.SA_AT_VELOCITY         # constant (zero) rate
            if self.move_complete:
                status |= P.SA_MOVE_COMPLETE
            return f"{status:04X}"
        if mnemonic == "SD":                       # SD1
            return f"{P.SD_ENABLED if self.enabled else 0:04X}"
        if mnemonic == "SS":
            return f"{P.SS_READY:04X}"
        if mnemonic == "MN":
            return "SmartStep23"
        if mnemonic == "VE":
            v = float(arg)
            if self.continuous and self.target is None:
                # inside an MC move VE±r (with GO) retargets the jog;
                # VE0 stops it — the manual's hosted-mode idiom
                self.continuous = v
                if v == 0.0:
                    self.moving = False
            self.velocity = abs(v) or self.velocity
            self._pending_ve = v
            return None
        if mnemonic == "AC" or mnemonic == "DE":
            self.accel = float(arg)
            return None
        if mnemonic == "DA":
            self._pending = ("DA", float(arg))
            return None
        if mnemonic == "DI":
            self._pending = ("DI", float(arg))
            return None
        if mnemonic == "MC":
            self._pending = ("MC", +1.0 if arg != "-" else -1.0)
            return None
        if mnemonic == "GO":
            pending = getattr(self, "_pending", None)
            self.move_complete = False
            if pending and pending[0] == "DA":
                self.target, self.continuous = pending[1], 0.0
                self.moving = True
            elif pending and pending[0] == "DI":
                self.target = self.position + pending[1]
                self.continuous, self.moving = 0.0, True
            elif pending and pending[0] == "MC":
                ve = getattr(self, "_pending_ve", self.velocity)
                self.continuous = ve if ve else self.velocity
                self.target = None
                self.moving = self.continuous != 0.0
            self._pending = None
            return None
        if mnemonic == "SP":
            self.position = float(arg or 0.0)
            return None
        if mnemonic == "EA":
            self.enabled = arg.strip() != "0"
            if not self.enabled:
                self._halt()
            return None
        if mnemonic in ("S", "K"):
            self._halt()
            return None
        if mnemonic == "CB":
            self._pending = None
            return None
        return None                                # unknown: swallow

    def _halt(self) -> None:
        self.target = None
        self.continuous = 0.0
        self.moving = False


class SimChain:
    """The whole daisy chain behind a Serial-shaped interface."""

    def __init__(self):
        self.drives: Dict[int, _SimDrive] = {
            u: _SimDrive(u) for u in (P.UNIT_Z, P.UNIT_Y, P.UNIT_X)}
        self._rx = bytearray()        # bytes waiting for the host
        self._lock = threading.Lock()
        self.is_open = True
        #: full command log, for tests ({(unit, mnemonic, arg), ...})
        self.log: List[tuple] = []

    # ── Serial surface ───────────────────────────────────────────────────
    def write(self, data: bytes) -> int:
        with self._lock:
            self._rx += data          # the chain echoes everything
            for token in data.decode("ascii", errors="replace").split("\r"):
                token = token.strip()
                if token:
                    self._dispatch(token)
        return len(data)

    def read(self, n: int = 1) -> bytes:
        with self._lock:
            out = bytes(self._rx[:n])
            del self._rx[:n]
        return out

    @property
    def in_waiting(self) -> int:
        with self._lock:
            return len(self._rx)

    def reset_input_buffer(self) -> None:
        with self._lock:
            self._rx.clear()

    def close(self) -> None:
        self.is_open = False

    # ── dispatch ─────────────────────────────────────────────────────────
    def _dispatch(self, token: str) -> None:
        unit: Optional[int] = None
        i = 0
        while i < len(token) and token[i].isdigit():
            i += 1
        if i:
            unit = int(token[:i])
            token = token[i:]
        if token in ("S", "K"):       # the two single-letter commands
            mnemonic, arg = token, ""
        elif len(token) < 2:          # everything else is two letters
            return
        else:
            mnemonic, arg = token[:2], token[2:]
        # PA1/SA1/SD1 carry the axis suffix in the argument — strip the
        # "1" the way the real drive expects it
        if mnemonic in ("PA", "SA", "SD") and arg.startswith("1"):
            arg = arg[1:]
        targets = ([self.drives[unit]] if unit in self.drives
                   else list(self.drives.values()) if unit is None
                   else [])
        for drive in targets:
            self.log.append((drive.unit, mnemonic, arg))
            payload = drive.handle(mnemonic, arg)
            if payload is not None:
                self._rx += b"*" + payload.encode("ascii") + P.CR
