"""lswt_traverse — Python interface to the South LSWT 3-axis traverse.

Three IDC SmartStep23 microstepping SmartDrives on one RS-232C daisy
chain (9600 8N1, XON/XOFF; unit 1 = Z vertical, 2 = Y lateral,
3 = X axial). The drives run their own motion profiles from buffered
IDeal commands (``AC/DE/VE/DA…GO``), so this driver is a thin
transactor + monitor — far simpler than the SWT WAGO traverse's
host-side bang-bang loop.

Referencing: NO homing routine. Jog to the reference spot, Set home
(wire ``SP``), and the host-side soft travel limits take it from there.

Layers
------
* :mod:`lswt_traverse.protocol`  - IDeal wire protocol + status bits
* :mod:`lswt_traverse.config`    - TraverseConfig / AxisConfig (JSON)
* :mod:`lswt_traverse.device`    - LswtTraverseDrive: 3-axis control
* :mod:`lswt_traverse.emulator`  - SimChain byte-level stand-in
* :mod:`lswt_traverse.app`       - PyQt6 GUI (imported lazily)
"""

from __future__ import annotations

from .about import __version__                        # noqa: F401

from .config import AxisConfig, TraverseConfig, defaults_path
from .device import AXES, LswtTraverseDrive
from .emulator import SimChain
from .protocol import ProtocolError

__all__ = [
    "AxisConfig", "TraverseConfig", "defaults_path",
    "LswtTraverseDrive", "AXES",
    "SimChain", "ProtocolError",
]
