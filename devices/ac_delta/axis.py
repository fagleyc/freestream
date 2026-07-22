"""One crescent axis over Modbus TCP (Delta C2000 + PLC step-speed model).

Register map (from the deployed C# ``FTool_SSWT_Sting``):

* **8714** (read, 1 reg)  — encoder position (signed 16-bit)
* **8193** (write single) — command register:
    - speed steps forward: 4370 4626 4882 5138 5394 (steps 1..5)
    - speed steps reverse: 4386 4642 4898 5154 5410
    - stop: 17 (Alpha) / 33 (Beta)

Improvements over the C# implementation: one persistent connection per
axis (the C# reconnected before every operation) and change-only command
writes (the C# rewrote the step every 100 ms tick).
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from .config import AxisConfig

log = logging.getLogger(__name__)

# The C2000 PLC occasionally answers late; pymodbus then logs
# "request ask for transaction_id=N but got id=N-1, Skipping" at ERROR and
# recovers by skipping the stale frame. That self-healing case is noise —
# real failures still surface through isError()/AxisError and the
# drive-level watchdog. Demote the library's logger accordingly.
logging.getLogger("pymodbus.logging").setLevel(logging.CRITICAL)
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)

# Wire (0-based) addresses — the C# FieldTalk library counts register
# references from 1, so every C# address is +1 vs the wire (confirmed by
# Casey's working LabVIEW):
#   encoder  = wire 8713 (C# "8714"); signed 16-bit, verified live
#   command  = wire 8192 (C# "8193") = Delta C2000 control word 0x2000
# Control word bits (C2000 manual "Control command (20xx)"):
#   bits1-0: 01=Stop 10=Run 11=Jog+Run | bits5-4: 01=FWD 10=REV
#   bits7-6: accel/decel set | bits11-8: step speed 0-15 | bit12: enable
# The C# "step values" are full control words: 0x1112=Run+FWD+step1+en …
REG_ENCODER = 8713
REG_COMMAND = 8192

FWD_STEPS = [4370, 4626, 4882, 5138, 5394]   # steps 1..5
REV_STEPS = [4386, 4642, 4898, 5154, 5410]


class AxisError(RuntimeError):
    pass


def _mb_call(fn, *args, unit_id: int, **kwargs):
    """Call a pymodbus method across 3.x kwarg renames (slave/device_id).

    Every transport failure is wrapped into AxisError — pymodbus raises
    its own exceptions (ModbusIOException, ConnectionException) on
    timeouts; unwrapped they escape the drive's watchdog (this killed
    the traverse control thread live on 2026-07-07).
    """
    for kw in ({"device_id": unit_id}, {"slave": unit_id}, {}):
        try:
            return fn(*args, **kwargs, **kw)
        except TypeError:
            continue
        except Exception as exc:                       # noqa: BLE001
            raise AxisError(f"{fn.__name__}: {exc}") from exc
    raise AxisError(f"pymodbus call {fn.__name__} incompatible")


class CrescentAxis:
    """Persistent Modbus client + protocol for one axis. Thread-safe."""

    def __init__(self, cfg: AxisConfig, timeout_s: float = 1.0):
        self.cfg = cfg
        self._timeout = timeout_s
        self._client = None
        self._lock = threading.Lock()
        self._last_command: Optional[int] = None

    # ── connection ───────────────────────────────────────────────────────
    def connect(self) -> None:
        from pymodbus.client import ModbusTcpClient
        with self._lock:
            try:
                self._client = ModbusTcpClient(self.cfg.ip,
                                               port=self.cfg.port,
                                               timeout=self._timeout,
                                               retries=2)
            except TypeError:       # older pymodbus without retries kwarg
                self._client = ModbusTcpClient(self.cfg.ip,
                                               port=self.cfg.port,
                                               timeout=self._timeout)
            if not self._client.connect():
                self._client = None
                raise AxisError(f"{self.cfg.name}: cannot connect to "
                                f"{self.cfg.ip}:{self.cfg.port}")
            self._last_command = None

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
    def read_encoder(self) -> int:
        """Raw signed encoder register (one silent retry on a hiccup)."""
        with self._lock:
            if self._client is None:
                raise AxisError(f"{self.cfg.name}: not connected")
            rr = _mb_call(self._client.read_holding_registers, REG_ENCODER,
                          count=1, unit_id=self.cfg.unit_id)
            if rr.isError():        # e.g. skipped stale transaction — retry
                rr = _mb_call(self._client.read_holding_registers,
                              REG_ENCODER, count=1, unit_id=self.cfg.unit_id)
            if rr.isError():
                raise AxisError(f"{self.cfg.name}: encoder read failed: {rr}")
            raw = rr.registers[0]
            return raw - 65536 if raw >= 32768 else raw

    def read_angle(self) -> float:
        return self.cfg.encoder_to_angle(self.read_encoder())

    def _write_command(self, value: int) -> None:
        with self._lock:
            if self._client is None:
                raise AxisError(f"{self.cfg.name}: not connected")
            rr = _mb_call(self._client.write_register, REG_COMMAND,
                          value=value, unit_id=self.cfg.unit_id)
            if rr.isError():
                raise AxisError(f"{self.cfg.name}: command write failed: {rr}")
            self._last_command = value

    def command_step(self, step: int, forward: bool) -> None:
        """Drive at speed step 1..5 in the given direction (change-only).

        ``forward`` is in ANGLE space; the Beta axis's wiring swaps the
        fwd/rev tables (`invert_direction`), exactly as the deployed C#.
        """
        if not 1 <= step <= 5:
            raise ValueError("step must be 1..5")
        table_forward = forward != self.cfg.invert_direction
        value = (FWD_STEPS if table_forward else REV_STEPS)[step - 1]
        if value != self._last_command:
            self._write_command(value)

    def stop(self) -> None:
        """Stop this axis (always written, never elided)."""
        self._write_command(self.cfg.stop_value)

    def last_command(self) -> Optional[int]:
        return self._last_command
