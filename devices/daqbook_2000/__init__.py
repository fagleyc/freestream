"""daqbook_2000 — Python interface to the IOtech DaqBook/2000-series DAQ.

A standalone driver for the DaqBook/2005 that measures the tunnel's dynamic
pressure (Pdiff), total pressure (Ptot) and temperature (Temp) voltages, via
the vendor DaqX API (``DaqX64.dll``) over Ethernet.

Layers
------
* :mod:`daqbook_2000.daqx`       - ctypes binding to DaqX64.dll (+ constants)
* :mod:`daqbook_2000.config`     - DaqbookConfig / ChannelConfig (JSON)
* :mod:`daqbook_2000.datamodel`  - ScanRingBuffer (dynamic channel fields)
* :mod:`daqbook_2000.device`     - Daqbook2000 driver (thread + sim fallback)
* :mod:`daqbook_2000.emulator`   - SimCore synthetic tunnel signals
* :mod:`daqbook_2000.aux_source` - AuxSource adapter for the ATE balance app
* :mod:`daqbook_2000.app`        - PyQt6 GUI (imported lazily; needs PyQt6)

The GUI is intentionally *not* imported here so the core can be used headless.
"""

from __future__ import annotations

from .about import __version__

from . import daqx
from .config import ChannelConfig, DaqbookConfig
from .datamodel import ScanRingBuffer
from .device import Daqbook2000
from .aux_source import DaqbookAuxSource

__all__ = [
    "daqx",
    "ChannelConfig", "DaqbookConfig",
    "ScanRingBuffer",
    "Daqbook2000",
    "DaqbookAuxSource",
]
