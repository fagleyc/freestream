"""TunnelControl — the ONLY path that writes to the tunnel. Guarded.

Strictly separated from :class:`~tunnel_plc.monitor.TunnelMonitor`
(which is read-only by construction): this class must be created with
``enable_writes=True`` explicitly, keyword-only, or it refuses to
instantiate. It borrows the monitor's gateway (one shared transport)
but adds no read capability of its own — every decision uses the
monitor's last snapshot.

Guards on every command:

* the monitor snapshot must be FRESH (a stale view of the tunnel means
  we don't know what we'd be commanding into),
* ``Inverter_Fault_Light`` must be clear,
* RPM setpoints are clamped to ``config.rpm_max`` and REFUSED entirely
  while ``rpm_max`` is 0 (not configured),
* every write is logged with timestamp, old value, new value.

Exception, deliberate and documented: the two **stop** buttons bypass
the fault/staleness guards (stopping machinery is the safe direction —
a fault is exactly when you want the stop to go through). They still
require a live gateway.

Fan buttons are momentary: write 1, hold ``button_hold_ms`` (250 ms),
write 0 — mirroring an operator pressing the HMI touchscreen button.
**TODO (physical test required):** verify against the real HMI that the
VersaMax latches on this pulse shape; ``config.momentary_verified``
records the outcome and every pulse logs a reminder until it is set.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Optional

from .config import TunnelConfig
from .gateway import GatewayError
from .monitor import TunnelMonitor
from .registers import BLOCK2_ADDR

log = logging.getLogger(__name__)


class WriteRefused(RuntimeError):
    """A guarded command was refused (reason in the message)."""


def _hint(exc: Exception) -> str:
    """Actionable advice for the known gateway write rejections."""
    text = str(exc)
    if "ILLEGAL DATA ADDRESS" in text:
        return ("  [Gateway rejected the write address: in Crimson 3 "
                "check that the Modbus TCP slave is NOT 'Read Only' and "
                "that the write block's direction is 'Block to Tag', "
                "then re-download to the G315]")
    if "ILLEGAL FUNCTION" in text:
        return ("  [Slave rejects this write function code — the driver "
                "already tries FC16 then FC6; check the Crimson Modbus "
                "slave settings]")
    return ""


@dataclass
class WriteRecord:
    """One write to the gateway, for the audit log."""
    t: float                       # time.time()
    tag: str
    old: object                    # value per the monitor before the write
    new: object
    note: str = ""


class TunnelControl:
    """Write path to the tunnel. Requires explicit ``enable_writes=True``."""

    def __init__(self, config: TunnelConfig, monitor: TunnelMonitor, *,
                 enable_writes: bool = False):
        if enable_writes is not True:
            raise PermissionError(
                "TunnelControl commands real machinery — construct it "
                "with enable_writes=True (keyword) only when writes are "
                "intended")
        self.config = config
        self.monitor = monitor
        self.gateway = monitor.gateway       # shared transport, no reads
        self.on_status: Optional[Callable[[str], None]] = None
        self.write_log: deque[WriteRecord] = deque(maxlen=1000)
        self._pulse_lock = threading.Lock()

    # ── guards ───────────────────────────────────────────────────────────
    def _guard(self, action: str, *, bypass_interlocks: bool = False):
        """Common pre-write checks; returns the fresh snapshot."""
        if not self.gateway.connected:
            raise WriteRefused(f"{action}: gateway not connected")
        snap = self.monitor.snapshot()
        if bypass_interlocks:
            return snap
        if snap.stale:
            raise WriteRefused(
                f"{action}: monitor data is STALE ({snap.age_s:.1f}s old) "
                f"— refusing to command blind")
        if snap.inverter_fault:
            raise WriteRefused(
                f"{action}: Inverter_Fault_Light is set — clear the "
                f"fault at the console first")
        return snap

    # ── RPM setpoint ─────────────────────────────────────────────────────
    def set_rpm(self, rpm: float) -> float:
        """Write the fan speed command. Returns the value actually sent
        (clamped to [0, rpm_max]). Refuses while rpm_max is unconfigured.
        """
        if self.config.rpm_max <= 0:
            raise WriteRefused(
                "set_rpm: rpm_max is not configured (0) — set a real "
                "limit in Settings/config before commanding speed")
        snap = self._guard("set_rpm")
        clamped = min(max(float(rpm), 0.0), self.config.rpm_max)
        if clamped != rpm:
            self._status(f"RPM request {rpm:g} clamped to {clamped:g} "
                         f"(rpm_max)")
        raw = int(round(clamped / self.config.rpm_scale))
        self._write("RPM_Set", raw, old=snap.rpm_set, new=clamped)
        return clamped

    # ── momentary fan buttons ────────────────────────────────────────────
    def start_tunnel_fan(self) -> None:
        snap = self._guard("start_tunnel_fan")
        self._pulse("Tunnel_Fan_Start_Button", old=snap.fan_running)

    def stop_tunnel_fan(self) -> None:
        """Stop bypasses fault/staleness guards — stopping is the safe
        direction; requires only a live gateway."""
        snap = self._guard("stop_tunnel_fan", bypass_interlocks=True)
        if snap.stale or snap.inverter_fault:
            self._status("stop_tunnel_fan: interlocks bypassed for a "
                         "STOP command")
        self._pulse("Tunnel_Fan_Stop_Button", old=snap.fan_running)

    def start_cooling_fan(self) -> None:
        snap = self._guard("start_cooling_fan")
        self._pulse("Cooling_Fan_Start_Button",
                    old=snap.cooling_fan_light_start)

    def stop_cooling_fan(self) -> None:
        """Stop bypasses fault/staleness guards (see stop_tunnel_fan)."""
        snap = self._guard("stop_cooling_fan", bypass_interlocks=True)
        if snap.stale or snap.inverter_fault:
            self._status("stop_cooling_fan: interlocks bypassed for a "
                         "STOP command")
        self._pulse("Cooling_Fan_Stop_Button",
                    old=snap.cooling_fan_light_start)

    # ── plumbing ─────────────────────────────────────────────────────────
    def _pulse(self, tag: str, old: object) -> None:
        """Momentary button: 1 → hold button_hold_ms → 0 (serialized)."""
        addr = BLOCK2_ADDR[tag]
        hold = max(self.config.button_hold_ms, 1) / 1000.0
        note = f"momentary pulse {self.config.button_hold_ms}ms"
        if not self.config.momentary_verified:
            note += " [TODO: pulse shape UNVERIFIED vs HMI button]"
            self._status(f"{tag}: momentary behavior not yet verified "
                         f"against the physical HMI — supervise this")
        with self._pulse_lock:
            try:
                self.gateway.write_element(addr, 1)
            except GatewayError as exc:
                raise WriteRefused(f"{tag}: press write failed: "
                                   f"{exc}{_hint(exc)}") from exc
            try:
                time.sleep(hold)
            finally:
                # the release write must always be attempted; if IT
                # fails after a successful press, the button may be
                # latched at 1 on the gateway — shout about it
                try:
                    self.gateway.write_element(addr, 0)
                except GatewayError as exc:
                    raise WriteRefused(
                        f"{tag}: RELEASE write failed — the button "
                        f"register may be stuck at 1, verify at the "
                        f"HMI! {exc}{_hint(exc)}") from exc
        self._log(tag, old=old, new="pulse(1→0)", note=note)
        self._status(f"{tag} pulsed")

    def _write(self, tag: str, raw: int, old: object, new: object) -> None:
        addr = BLOCK2_ADDR[tag]
        try:
            self.gateway.write_element(addr, raw)
        except GatewayError as exc:
            raise WriteRefused(f"{tag}: write failed: "
                               f"{exc}{_hint(exc)}") from exc
        self._log(tag, old=old, new=new)
        self._status(f"{tag} = {new} (was {old})")

    def _log(self, tag: str, old: object, new: object,
             note: str = "") -> None:
        rec = WriteRecord(t=time.time(), tag=tag, old=old, new=new,
                          note=note)
        self.write_log.append(rec)
        log.info("WRITE %s: %s -> %s %s", tag, old, new, note)

    def _status(self, msg: str) -> None:
        log.info(msg)
        if self.on_status:
            self.on_status(msg)
