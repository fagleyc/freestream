"""NI USB-6351 (X series) device driver for the wind tunnel suite.

Layers
------
``config``     — :class:`NiDaqConfig` + per-channel/AO/trigger dataclasses,
                 JSON save/load
``datamodel``  — :class:`ScanRingBuffer` (device-owned, thread-safe)
``device``     — :class:`NiUsb6351` threaded driver (nidaqmx)
``emulator``   — synthetic signals for ``force_sim`` operation
``balcal``     — balance ``.vol`` calibration → body-frame forces
``app``        — standalone PyQt6 GUI (``python -m ni_usb_6351.app``)
"""

from .about import __version__                        # noqa: F401

from .config import (AOChannelConfig, ChannelConfig, NiDaqConfig,   # noqa
                     TriggerConfig)
from .datamodel import ScanRingBuffer                               # noqa
from .device import NiUsb6351                                       # noqa
