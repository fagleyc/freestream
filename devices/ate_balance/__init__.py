"""ate_balance — Python interface to the ATE 6-component underfloor balance.

A standalone client ("TMS") for the ATE Aerodynamic Test Equipment external
balance, speaking the TCP/UDP protocol of the balance's OGI control PC
(Operations Manual AID-010-10015-1, section 6).

Layers
------
* :mod:`ate_balance.protocol`   - pure wire protocol (framing, LOADS codec)
* :mod:`ate_balance.datamodel`  - BalanceFrame / MasterFrame / RingBuffer /
                                  TestCase (mirrors wtdaq + Streamlined)
* :mod:`ate_balance.reduction`  - frame merging + dwell averaging (raw loads)
* :mod:`ate_balance.config`     - AteConfig (endpoints, rated maxima, JSON)
* :mod:`ate_balance.device`     - AteBalanceDevice (sockets + threads + sim)
* :mod:`ate_balance.emulator`   - FakeOGI, a pure-Python OGI stand-in
* :mod:`ate_balance.aux_source`        - AuxSource ABC (DAQbook integration point)
* :mod:`ate_balance.app`        - PyQt6 GUI (imported lazily; needs PyQt6)

The GUI is intentionally *not* imported here so the core can be used headless.
"""

from __future__ import annotations

__version__ = "0.1.0"

from . import protocol, reduction
from .config import AteConfig
from .datamodel import (BalanceFrame, MasterFrame, RingBuffer, ReducedPoint,
                        TestCase, TunnelConditions, FIELDS)
from .device import AteBalanceDevice
from .emulator import FakeOGI

__all__ = [
    "protocol", "reduction",
    "AteConfig",
    "BalanceFrame", "MasterFrame", "RingBuffer", "ReducedPoint",
    "TestCase", "TunnelConditions", "FIELDS",
    "AteBalanceDevice", "FakeOGI",
]
