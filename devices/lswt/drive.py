"""Modbus TCP protocol layer for the ABB ACS530 LSWT fan drive.

Register map (deployed C# truth:
``Tool_LSWT_Flow_Velocity\\HwControllerVelocityLSWT_ACB530.cs``, which
used FieldTalk 1-BASED register addressing → wire address =
FieldTalk − 1, the same +1 lesson as this repo's other C# ports):

* **Control register — wire 0** (C# ``Modbus_ABB530_Control_Register_
  Address = 1``, line 233). Write ``1150`` = STOP, ``1151`` = START
  (ABB drives-profile control words; C# lines 237–238). FC6.
* **Reference register — wire 1** (C# line 234). ``0–20000`` scales
  0 → full speed (full speed = 60 Hz motor). **The C# wrote the
  NEGATIVE of the scaled value** (line 191:
  ``writeSingleRegister(slave, 2, (short)-rpmScaledTo20000)``) — a
  direction convention on these fans, preserved via
  ``reference_sign = -1``. VERIFY on first live run.
* **Actual-speed register — wire 102** (C# line 235). Current output
  frequency × 10 (0–600 = 0–60.0 Hz), read signed (a negative
  reference may report a negative frequency).

The C# reconnected per transaction (open → one register → close, every
time — a big part of why it felt slow). This layer holds ONE
persistent pymodbus client, thread-safe, and wraps EVERY pymodbus
error into :class:`LswtError` (an unwrapped pymodbus exception killed
the traverse control thread live on 2026-07-07 — never again).
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)

# demote pymodbus' recovered-transaction ERROR chatter (see traverse)
logging.getLogger("pymodbus.logging").setLevel(logging.CRITICAL)
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)

# ── wire (0-based) register addresses — FieldTalk value − 1 ─────────────
REG_CONTROL = 0          # FieldTalk 1: stop/start control words (FC6)
REG_REFERENCE = 1        # FieldTalk 2: 0–20000 = 0–full speed (FC6)
REG_ACTUAL_HZ_X10 = 102  # FieldTalk 103: output frequency × 10 (FC3)

# ABB drives-profile control words (C# lines 237–238)
CMD_STOP = 1150
CMD_START = 1151

REF_FULL_COUNTS = 20000  # reference full scale
FULL_SPEED_HZ = 60.0     # …corresponds to 60 Hz motor


class LswtError(RuntimeError):
    """Any LSWT drive comm/protocol failure (typed wrapper)."""


def reference_counts(hz: float) -> int:
    """Reference register counts (0–20000, unsigned magnitude) for a
    motor frequency. The direction sign is applied at write time."""
    counts = round(REF_FULL_COUNTS * hz / FULL_SPEED_HZ)
    return max(0, min(REF_FULL_COUNTS, int(counts)))


def _signed16(v: int) -> int:
    return v - 0x1_0000 if v >= 0x8000 else v


def _mb_call(fn, *args, unit_id: int, **kwargs):
    """Call a pymodbus method across 3.x kwarg renames (slave/device_id).

    EVERY transport failure is wrapped into LswtError — pymodbus raises
    its own exceptions (ModbusIOException, ConnectionException) on
    timeouts, and an unwrapped one killed a control thread live once.
    """
    for kw in ({"device_id": unit_id}, {"slave": unit_id}, {}):
        try:
            return fn(*args, **kwargs, **kw)
        except TypeError:
            continue
        except Exception as exc:                       # noqa: BLE001
            raise LswtError(f"{fn.__name__}: {exc}") from exc
    raise LswtError(f"pymodbus call {fn.__name__} incompatible")


class AbbAcs530:
    """Persistent Modbus TCP client for one ACS530 drive. Thread-safe."""

    def __init__(self, ip: str, port: int = 502, unit_id: int = 1,
                 timeout_s: float = 2.0, reference_sign: int = -1):
        self.ip = ip
        self.port = port
        self.unit_id = unit_id
        self._timeout = timeout_s
        self.reference_sign = 1 if reference_sign >= 0 else -1
        self._client = None
        self._lock = threading.Lock()

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
                raise LswtError(f"cannot connect to ACS530 at "
                                f"{self.ip}:{self.port}")

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
    def read_actual_hz(self) -> float:
        """Current output frequency in Hz (wire 102, register = Hz×10).

        Signed decode: with the negative reference convention the drive
        may report a negative frequency — callers wanting the speed
        magnitude take ``abs()``.
        """
        with self._lock:
            if self._client is None:
                raise LswtError("not connected")
            rr = _mb_call(self._client.read_holding_registers,
                          REG_ACTUAL_HZ_X10, count=1,
                          unit_id=self.unit_id)
            if rr.isError():
                raise LswtError(f"actual-Hz read failed: {rr}")
            return _signed16(rr.registers[0]) / 10.0

    def write_reference(self, counts: int) -> None:
        """Write the speed reference (wire 1, FC6).

        ``counts`` is the unsigned magnitude 0–20000 (clamped here);
        ``reference_sign`` is applied on the wire — the C# wrote the
        NEGATIVE of the scaled value (line 191), preserved by the
        default sign of −1. VERIFY on first live run at a tiny value.
        """
        counts = max(0, min(REF_FULL_COUNTS, int(counts)))
        value = (self.reference_sign * counts) & 0xFFFF
        with self._lock:
            if self._client is None:
                raise LswtError("not connected")
            rr = _mb_call(self._client.write_register, REG_REFERENCE,
                          value=value, unit_id=self.unit_id)
            if rr.isError():
                raise LswtError(f"reference write failed: {rr}")

    def write_control(self, word: int) -> None:
        """Write a control word (wire 0, FC6): CMD_START / CMD_STOP."""
        with self._lock:
            if self._client is None:
                raise LswtError("not connected")
            rr = _mb_call(self._client.write_register, REG_CONTROL,
                          value=int(word) & 0xFFFF, unit_id=self.unit_id)
            if rr.isError():
                raise LswtError(f"control write failed: {rr}")

    def start(self) -> None:
        self.write_control(CMD_START)

    def stop(self) -> None:
        self.write_control(CMD_STOP)
