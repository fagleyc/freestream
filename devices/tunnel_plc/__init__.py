"""tunnel_plc — Freestream interface to the SSWT tunnel (Red Lion G315).

The tunnel is run by a Red Lion G315 HMI at 192.168.1.50 bridging a GE
VersaMax PLC (Ethernet SRTP) and a GE FanDrive (RS-485 SNP). Freestream
talks ONLY to the G315's Modbus TCP slave (port 502, unit 1) — Crimson
L4 gateway blocks re-export the PLC tags as 32-bit register pairs.

Two strictly separated classes:

* :class:`~tunnel_plc.monitor.TunnelMonitor` — read-only Block1 poller
  (atomic snapshot, reconnect/backoff, staleness flag). No writes.
* :class:`~tunnel_plc.control.TunnelControl` — the only write path;
  requires ``enable_writes=True`` explicitly, clamps RPM to a
  configured maximum, refuses on fault/stale data, pulses the momentary
  fan buttons, and logs every write.

Plus a plant simulator for hardware-free development. Derived aero
quantities (Mach/Reynolds) are out of scope — the gateway exposes no
analog channels; Freestream computes those from its own DAQ devices.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .config import TunnelConfig
from .control import TunnelControl, WriteRecord, WriteRefused
from .gateway import GatewayError, ModbusGateway
from .monitor import TunnelMonitor
from .registers import (BLOCK1_ADDR, BLOCK1_TAGS, BLOCK2_ADDR,
                        TunnelSnapshot)

__all__ = [
    "TunnelConfig", "TunnelMonitor", "TunnelControl",
    "TunnelSnapshot", "WriteRecord", "WriteRefused",
    "ModbusGateway", "GatewayError",
    "BLOCK1_ADDR", "BLOCK1_TAGS", "BLOCK2_ADDR",
]
