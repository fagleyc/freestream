"""ac_delta — Python interface to the ARC Crescent (SSWT sting) drive.

Dual Delta C2000 drives over Modbus TCP (Alpha 192.168.1.11, Beta
192.168.1.12) running the host-side step-speed position loop from the
deployed C# tooling, improved: persistent connections, change-only writes,
configurable loop rate and deceleration bands, and synchronous dual-axis
moves.

Layers
------
* :mod:`ac_delta.axis`      - CrescentAxis: Modbus protocol for one drive
* :mod:`ac_delta.config`    - CrescentConfig / AxisConfig (JSON, cal)
* :mod:`ac_delta.device`    - CrescentDrive: dual-axis position loop
* :mod:`ac_delta.emulator`  - SimAxis physics stand-in
* :mod:`ac_delta.app`       - PyQt6 GUI (imported lazily)
"""

from __future__ import annotations

from .about import __version__

from .axis import CrescentAxis, AxisError, FWD_STEPS, REV_STEPS
from .config import AxisConfig, CrescentConfig
from .device import CrescentDrive

__all__ = [
    "CrescentAxis", "AxisError", "FWD_STEPS", "REV_STEPS",
    "AxisConfig", "CrescentConfig",
    "CrescentDrive",
]
