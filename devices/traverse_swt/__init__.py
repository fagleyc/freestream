"""traverse_swt — Python interface to the SSWT 3-axis traverse.

WAGO 750 PLC at 192.168.1.21:502 (Modbus TCP) driving three 750-673
stepper modules (X axial, Y lateral, Z vertical). Improvements over the
deployed C# tooling: one persistent connection (the C# reconnected
before every operation), one block read per tick, change-only command
writes, host-side calibrated positioning and a 1M-count rollover unwrap.

Layers
------
* :mod:`traverse_swt.plc`       - WagoTraversePlc: Modbus protocol
* :mod:`traverse_swt.config`    - TraverseConfig / AxisConfig (JSON, cal)
* :mod:`traverse_swt.device`    - TraverseDrive: 3-axis control loop
* :mod:`traverse_swt.emulator`  - SimPlc plant stand-in
* :mod:`traverse_swt.app`       - PyQt6 GUI (imported lazily)
"""

from __future__ import annotations

from .about import __version__                        # noqa: F401

from .config import AxisConfig, TraverseConfig, slopes_from_legacy_xml
from .device import HomingResult, TraverseDrive
from .plc import (STATUS_LIMIT_MASK, BlockReading, PlcError,
                  WagoTraversePlc)

__all__ = [
    "AxisConfig", "TraverseConfig", "slopes_from_legacy_xml",
    "TraverseDrive", "HomingResult",
    "WagoTraversePlc", "PlcError", "BlockReading",
    "STATUS_LIMIT_MASK",
]
