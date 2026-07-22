"""strainbook_616 — Python interface to the IOtech StrainBook/616.

A standalone driver for the 8-channel strain-gage system reading the
internal balance bridges (N1, N2, Y1, Y2, Axial, Roll) + excitation at the
USAFA subsonic tunnel, via the vendor DaqX API over Ethernet.

Layers (same architecture as the other AeroVIS device packages)
-----
* :mod:`strainbook_616.daqx`       - ctypes binding + WBK16 option constants
* :mod:`strainbook_616.config`     - StrainbookConfig / StrainChannelConfig
* :mod:`strainbook_616.datamodel`  - ScanRingBuffer (dynamic channel fields)
* :mod:`strainbook_616.device`     - Strainbook616 driver (thread + sim)
* :mod:`strainbook_616.emulator`   - SimCore synthetic bridge signals
* :mod:`strainbook_616.app`        - PyQt6 GUI (imported lazily)
"""

from __future__ import annotations

__version__ = "0.1.0"

from . import daqx
from .config import StrainChannelConfig, StrainbookConfig
from .datamodel import ScanRingBuffer
from .device import Strainbook616

__all__ = [
    "daqx",
    "StrainChannelConfig", "StrainbookConfig",
    "ScanRingBuffer",
    "Strainbook616",
]
