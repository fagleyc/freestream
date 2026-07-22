"""LSWT Sting device driver for the wind tunnel suite.

Dual-axis (alpha/beta) serial stepper-indexer drive, protocol recovered
from the deployed C# ``Tool_LSWT_Sting``/``Core.exe``.

Layers
------
``config``    — :class:`StingConfig` + per-axis dataclasses, JSON save/load
``protocol``  — :class:`StingProtocol` RS-232 command/response layer
``datamodel`` — :class:`ScanRingBuffer` (device-owned, thread-safe)
``device``    — :class:`StingDrive` threaded driver (poll loop, faults)
``emulator``  — :class:`SimSerial` wire-level drive emulator
``app``       — standalone PyQt6 GUI (``python -m lswt_sting.app``)
"""

__version__ = "0.1.0"

from .config import StingAxisConfig, StingConfig      # noqa: F401
from .datamodel import ScanRingBuffer                 # noqa: F401
from .device import StingDrive                        # noqa: F401
from .protocol import StingError, StingProtocol       # noqa: F401
