"""Modbus TCP protocol for the SSWT traverse WAGO 750 PLC.

One persistent connection serves all three axes (the deployed C#
reconnected before EVERY read and write — a big part of why it felt
sluggish). Each poll is a single 16-register block read that returns the
ControlWord echo, the StatusWord and all three DINT positions in one
transaction.

The StatusWord at %MW1 is LIVE again (2026-07): the rig's limit
switches were fixed (inverted at the module) and the PLC copies them
into StatusWord bits 0/1/2 — bit0 = X/Axial, bit1 = Y/Lateral,
bit2 = Z/Vertical, all NEGATIVE-direction switches. The module
hardware-limit lockout is UNLINKED (``Ptr_LimitSwitch = 0``), so the
drive never hard-stops on a limit by itself — the HOST watches the bit
and drops the jog (see device.py).

Wire (0-based) addresses. The C# FieldTalk library counts registers from
1, so every C# address is +1 vs the wire (same lesson as the AC Delta):
C# wrote its command to "12289" = wire 12288 = the PLC's %MW0.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

# The WAGO occasionally answers late; pymodbus then logs a recovered
# "transaction_id mismatch, Skipping" at ERROR while healing itself.
# Real failures still surface through isError()/PlcError and the
# drive-level watchdog. Demote the library's logger accordingly.
logging.getLogger("pymodbus.logging").setLevel(logging.CRITICAL)
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)

REG_CONTROL = 12288        # %MW0 — ControlWord (write FC6)
REG_STATUS = 12289         # %MW1 — StatusWord (limit-switch bits, live)
REG_BLOCK_START = 12288    # one FC3 read covers %MW0..%MW15
REG_BLOCK_COUNT = 16

# StatusWord limit-switch bits (%MW1): the NEGATIVE-direction switch of
# each axis. The module lockout is unlinked (Ptr_LimitSwitch = 0), so
# these bits are informational to the PLC — the host is the only layer
# that reacts (stop the jog / drive the homing sequence).
STATUS_LIMIT_MASK = {"X": 0x0001, "Y": 0x0002, "Z": 0x0004}

# word offsets of the DINT position low words within the block
_POS_WORD = {"X": 10, "Y": 12, "Z": 14}

# The physical INPUT process image is served at Modbus address 0 on
# WAGO 750 controllers. The three 750-673 stepper modules occupy 12
# input bytes each (X %IB0.., Y %IB12.., Z %IB24..); per the CoDeSys
# stepper library, input byte [11] is Status 1, [10] Status 2, [9]
# Status 3 (module state / error flags — the source of the MC3
# BasicError/BasicBusy bits).
REG_INPUT_START = 0
REG_INPUT_WORDS = 18       # 3 modules × 12 bytes
_MODULE_BASE_BYTE = {"X": 0, "Y": 12, "Z": 24}

# NOTE (2026-07): the modules are now configured to roll their position
# counter over cleanly at 1,000,000 counts, so the old MC3_SetPosition
# counter re-reference protocol (%MW2–%MW9) is retired — the module
# never stalls at a counter limit anymore. The host unwraps the 0…1M
# ring into a continuous absolute position instead (device._apply_counts).


class PlcError(RuntimeError):
    pass


def _input_byte(regs: List[int], i: int) -> int:
    """%IBi from the input-image registers (WAGO packs low byte first)."""
    reg = regs[i // 2]
    return (reg >> 8) & 0xFF if i % 2 else reg & 0xFF


def decode_module_status(regs: List[int]) -> Dict[str, Tuple[int, ...]]:
    """Per-axis (S1, S2, S3) stepper-module status bytes."""
    out = {}
    for ax, base in _MODULE_BASE_BYTE.items():
        out[ax] = (_input_byte(regs, base + 11),
                   _input_byte(regs, base + 10),
                   _input_byte(regs, base + 9))
    return out


@dataclass
class BlockReading:
    """One poll of the PLC: control echo, status word, positions."""
    control: int                   # ControlWord as the PLC sees it
    status: int                    # StatusWord (raw; limit bits 0/1/2)
    counts: Dict[str, int]         # {"X": …, "Y": …, "Z": …} raw ring
    # per-axis 750-673 status bytes (S1, S2, S3); None if unavailable
    module_status: Optional[Dict[str, Tuple[int, ...]]] = None


def _dint(lo: int, hi: int) -> int:
    """CoDeSys DINT from two %MW words, low word first, signed."""
    v = (hi << 16) | lo
    return v - 0x1_0000_0000 if v >= 0x8000_0000 else v


def _mb_call(fn, *args, unit_id: int, **kwargs):
    """Call a pymodbus method across 3.x kwarg renames (slave/device_id).

    EVERY transport failure is wrapped into PlcError — pymodbus raises
    its own exceptions (ModbusIOException, ConnectionException) on
    timeouts, and an unwrapped one killed the control thread live on
    2026-07-07, leaving the axes unsupervised. Never again.
    """
    for kw in ({"device_id": unit_id}, {"slave": unit_id}, {}):
        try:
            return fn(*args, **kwargs, **kw)
        except TypeError:
            continue
        except Exception as exc:                       # noqa: BLE001
            raise PlcError(f"{fn.__name__}: {exc}") from exc
    raise PlcError(f"pymodbus call {fn.__name__} incompatible")


class WagoTraversePlc:
    """Persistent Modbus client for the traverse PLC. Thread-safe."""

    def __init__(self, ip: str, port: int = 502, unit_id: int = 1,
                 timeout_s: float = 1.0):
        self.ip = ip
        self.port = port
        self.unit_id = unit_id
        self._timeout = timeout_s
        self._client = None
        self._lock = threading.Lock()
        self._last_control: Optional[int] = None
        # input-image (module status) support: degrade after repeated
        # failures instead of poisoning the main poll
        self._mod_status_fails = 0
        self.module_status_supported = True

    # ── connection ───────────────────────────────────────────────────────
    def connect(self) -> None:
        from pymodbus.client import ModbusTcpClient
        with self._lock:
            try:
                self._client = ModbusTcpClient(self.ip, port=self.port,
                                               timeout=self._timeout,
                                               retries=2)
            except TypeError:       # older pymodbus without retries kwarg
                self._client = ModbusTcpClient(self.ip, port=self.port,
                                               timeout=self._timeout)
            if not self._client.connect():
                self._client = None
                raise PlcError(f"cannot connect to WAGO at "
                               f"{self.ip}:{self.port}")
            self._last_control = None

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:                       # noqa: BLE001
                    pass
                self._client = None

    @property
    def connected(self) -> bool:
        return self._client is not None

    # ── protocol ─────────────────────────────────────────────────────────
    def read_block(self) -> BlockReading:
        """Control echo + status + all positions (one silent retry)."""
        with self._lock:
            if self._client is None:
                raise PlcError("not connected")
            rr = _mb_call(self._client.read_holding_registers,
                          REG_BLOCK_START, count=REG_BLOCK_COUNT,
                          unit_id=self.unit_id)
            if rr.isError():        # e.g. skipped stale transaction — retry
                rr = _mb_call(self._client.read_holding_registers,
                              REG_BLOCK_START, count=REG_BLOCK_COUNT,
                              unit_id=self.unit_id)
            if rr.isError():
                raise PlcError(f"block read failed: {rr}")
            regs = rr.registers

            module_status = None
            if self.module_status_supported:
                mr = _mb_call(self._client.read_holding_registers,
                              REG_INPUT_START, count=REG_INPUT_WORDS,
                              unit_id=self.unit_id)
                if mr.isError():
                    self._mod_status_fails += 1
                    if self._mod_status_fails >= 3:
                        self.module_status_supported = False
                        log.warning("input-image read @0 rejected %d× — "
                                    "module status disabled: %s",
                                    self._mod_status_fails, mr)
                else:
                    self._mod_status_fails = 0
                    module_status = decode_module_status(mr.registers)
        counts = {ax: _dint(regs[w], regs[w + 1])
                  for ax, w in _POS_WORD.items()}
        return BlockReading(control=regs[0], status=regs[1], counts=counts,
                            module_status=module_status)

    def read_status(self) -> int:
        """StatusWord only (limit switch bits) — cheap single-register."""
        with self._lock:
            if self._client is None:
                raise PlcError("not connected")
            rr = _mb_call(self._client.read_holding_registers, REG_STATUS,
                          count=1, unit_id=self.unit_id)
            if rr.isError():
                raise PlcError(f"status read failed: {rr}")
            return rr.registers[0]

    def write_control(self, word: int, force: bool = False) -> None:
        """Write the ControlWord (change-only unless ``force``).

        Stop paths must pass ``force=True`` so a stop is ALWAYS written
        even if the shadow says it already was.
        """
        word &= 0xFFFF
        with self._lock:
            if not force and word == self._last_control:
                return
            if self._client is None:
                raise PlcError("not connected")
            rr = _mb_call(self._client.write_register, REG_CONTROL,
                          value=word, unit_id=self.unit_id)
            if rr.isError():
                raise PlcError(f"control write failed: {rr}")
            self._last_control = word

    def last_control(self) -> Optional[int]:
        return self._last_control
