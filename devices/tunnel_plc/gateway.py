"""Modbus TCP transport to the Red Lion G315 gateway. Thread-safe.

One persistent connection shared by TunnelMonitor (reads) and
TunnelControl (writes) — the class carries no policy, only transport:
element-block reads and single-element writes with the configured
32-bit word order. All policy (poll cadence, staleness, write guards)
lives in the monitor/control classes.
"""

from __future__ import annotations

import logging
import threading
from typing import List, Optional

from .registers import decode_u32, encode_u32

log = logging.getLogger(__name__)

# The gateway occasionally answers late; pymodbus logs the recovered
# "transaction_id mismatch, Skipping" case at ERROR while healing itself.
# Real failures still surface via isError()/GatewayError.
logging.getLogger("pymodbus.logging").setLevel(logging.CRITICAL)
logging.getLogger("pymodbus").setLevel(logging.CRITICAL)


class GatewayError(RuntimeError):
    pass


_MODBUS_EXCEPTIONS = {
    1: "ILLEGAL FUNCTION (slave does not support this function code)",
    2: "ILLEGAL DATA ADDRESS (registers not readable/writable at this "
       "address)",
    3: "ILLEGAL DATA VALUE",
    4: "SLAVE DEVICE FAILURE",
}


def _describe(rr) -> str:
    """Readable text for a pymodbus error/exception response."""
    exc = getattr(rr, "exception_code", None)
    if exc is not None:
        fc = getattr(rr, "function_code", 0) & 0x7F
        return (f"Modbus exception {exc} on FC{fc}: "
                f"{_MODBUS_EXCEPTIONS.get(exc, 'unknown')}")
    return str(rr)


def _mb_call(fn, *args, unit_id: int, **kwargs):
    """Call a pymodbus method across 3.x kwarg renames (slave/device_id).

    Every transport failure is wrapped into GatewayError — pymodbus
    raises its own exceptions (ModbusIOException, ConnectionException)
    on timeouts, and an unwrapped one escapes the callers' error
    handling (this killed the traverse control thread live).
    """
    for kw in ({"device_id": unit_id}, {"slave": unit_id}, {}):
        try:
            return fn(*args, **kwargs, **kw)
        except TypeError:
            continue
        except Exception as exc:                       # noqa: BLE001
            raise GatewayError(f"{fn.__name__}: {exc}") from exc
    raise GatewayError(f"pymodbus call {fn.__name__} incompatible")


class ModbusGateway:
    """Persistent pymodbus client wrapper for the G315 Modbus slave."""

    def __init__(self, ip: str, port: int = 502, unit_id: int = 1,
                 timeout_s: float = 2.0, word_order: str = "low_first"):
        self.ip = ip
        self.port = port
        self.unit_id = unit_id
        self.word_order = word_order
        self._timeout = timeout_s
        self._client = None
        self._lock = threading.Lock()
        # write function preference: None = try FC16 first, fall back to
        # two FC6 singles if the slave rejects FC16; then remembered.
        self._write_fc: Optional[int] = None

    # ── connection ───────────────────────────────────────────────────────
    def connect(self) -> None:
        from pymodbus.client import ModbusTcpClient
        with self._lock:
            if self._client is not None:
                return
            try:
                client = ModbusTcpClient(self.ip, port=self.port,
                                         timeout=self._timeout, retries=2)
            except TypeError:       # older pymodbus without retries kwarg
                client = ModbusTcpClient(self.ip, port=self.port,
                                         timeout=self._timeout)
            if not client.connect():
                raise GatewayError(f"cannot connect to Red Lion at "
                                   f"{self.ip}:{self.port}")
            self._client = client

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
    def read_registers(self, address: int, count: int) -> List[int]:
        """One contiguous FC3 read (one silent retry on a hiccup)."""
        with self._lock:
            if self._client is None:
                raise GatewayError("not connected")
            rr = _mb_call(self._client.read_holding_registers, address,
                          count=count, unit_id=self.unit_id)
            if rr.isError():        # e.g. skipped stale transaction — retry
                rr = _mb_call(self._client.read_holding_registers, address,
                              count=count, unit_id=self.unit_id)
            if rr.isError():
                raise GatewayError(f"read @{address} x{count} failed: {rr}")
            return list(rr.registers)

    def read_elements(self, address: int, n_elements: int) -> List[int]:
        """N consecutive L4 elements as signed 32-bit ints (atomic read)."""
        regs = self.read_registers(address, n_elements * 2)
        return [decode_u32(regs[2 * i], regs[2 * i + 1], self.word_order)
                for i in range(n_elements)]

    def write_element(self, address: int, value: int) -> None:
        """Write ONE L4 element (two registers).

        Tries FC16 (write multiple) first; if the slave rejects the
        function, falls back to two FC6 single-register writes and
        remembers the working function. (For this map the high word is
        0 in practice, so the FC6 pair has no meaningful tearing
        window.)
        """
        pair = list(encode_u32(value, self.word_order))
        with self._lock:
            if self._client is None:
                raise GatewayError("not connected")
            if self._write_fc != 6:
                rr = _mb_call(self._client.write_registers, address,
                              values=pair, unit_id=self.unit_id)
                if not rr.isError():
                    self._write_fc = 16
                    return
                err16 = _describe(rr)
                if self._write_fc == 16:    # FC16 known-good, real fault
                    raise GatewayError(f"write @{address} = {value} "
                                       f"failed: {err16}")
                log.info("FC16 write rejected (%s) — trying FC6 singles",
                         err16)
            else:
                err16 = None
            for offset, reg_val in enumerate(pair):
                rr = _mb_call(self._client.write_register,
                              address + offset, value=reg_val,
                              unit_id=self.unit_id)
                if rr.isError():
                    detail = _describe(rr)
                    if err16 is not None:
                        detail = (f"FC16: {err16}; FC6 fallback: "
                                  f"{detail}")
                    raise GatewayError(f"write @{address} = {value} "
                                       f"failed: {detail}")
            if self._write_fc is None:
                log.info("slave accepts FC6 singles — using FC6 from "
                         "now on")
            self._write_fc = 6


class FakeClient:
    """Recording stand-in for pymodbus's client — unit tests only.

    ``read_map`` maps address → list of register values; every call is
    appended to ``calls`` as (method, address, count_or_values, unit).
    """

    def __init__(self, read_map: Optional[dict] = None):
        self.read_map = read_map or {}
        self.calls: list = []
        self.fail_reads = False
        # simulate a slave that rejects FC16 with this exception code
        # (e.g. 2 = illegal data address, as the G315 did live)
        self.reject_fc16: Optional[int] = None
        self.reject_fc6: Optional[int] = None

    class _RR:
        def __init__(self, registers=None, error=False,
                     function_code=0, exception_code=None):
            self.registers = registers or []
            self._error = error
            self.function_code = function_code
            self.exception_code = exception_code

        def isError(self):
            return self._error

    def connect(self):
        return True

    def close(self):
        pass

    def read_holding_registers(self, address, count=1, **kw):
        unit = kw.get("device_id", kw.get("slave"))
        self.calls.append(("read", address, count, unit))
        if self.fail_reads or address not in self.read_map:
            return self._RR(error=True)
        return self._RR(self.read_map[address][:count])

    def write_registers(self, address, values=None, **kw):
        unit = kw.get("device_id", kw.get("slave"))
        self.calls.append(("write", address, list(values or []), unit))
        if self.reject_fc16 is not None:
            return self._RR(error=True, function_code=0x90,
                            exception_code=self.reject_fc16)
        return self._RR()

    def write_register(self, address, value=None, **kw):
        unit = kw.get("device_id", kw.get("slave"))
        self.calls.append(("write6", address, value, unit))
        if self.reject_fc6 is not None:
            return self._RR(error=True, function_code=0x86,
                            exception_code=self.reject_fc6)
        return self._RR()
