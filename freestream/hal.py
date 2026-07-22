"""Hardware Abstraction Layer — the capability contracts every device
adapter implements.

The orchestrator, recorder and GUI are written against THESE types only;
concrete drivers (ac_delta, strainbook_616, daqbook_2000, ate_balance,
tunnel_plc, traverse_swt) are wrapped by thin adapters in
:mod:`freestream.adapters` and never modified. Adding hardware = new
adapter + one manifest entry.

Capabilities are runtime-checkable Protocols; an adapter declares what it
can do simply by implementing the methods. `DeviceManager` discovers
capabilities via ``isinstance``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol, runtime_checkable

import numpy as np

# ── status / specs ───────────────────────────────────────────────────────
OK = "OK"
OFFLINE = "OFFLINE"
FAULT = "FAULT"


@dataclass
class DeviceStatus:
    """Traffic-light state for the device rail and the record interlock."""
    state: str = OFFLINE                  # OK | OFFLINE | FAULT
    message: str = ""
    sim: bool = False
    last_sample_age_s: Optional[float] = None   # None = not streaming

    @property
    def ok(self) -> bool:
        return self.state == OK


@dataclass(frozen=True)
class ChannelSpec:
    """One streamed channel. ``group`` maps to the HDF5 group (§5.3)."""
    name: str                             # e.g. "N1", "Pdiff", "Alpha"
    unit: str                             # raw unit as streamed ("V", "deg")
    group: str                            # "StrainBook_0", "DaqBook2005", …
    kind: str                             # raw | tunnel | position | derived
    device_id: str = ""


@dataclass(frozen=True)
class AxisSpec:
    """One positioner axis with its soft limits and settled tolerance."""
    name: str                             # "alpha", "beta", "x", …
    unit: str
    min: float
    max: float
    tolerance: float


@dataclass
class MoveHandle:
    """Returned by Positioner.move_to — poll ``positioner.settled()``."""
    targets: Dict[str, float] = field(default_factory=dict)
    t_started: float = field(default_factory=time.time)


# ── capabilities ─────────────────────────────────────────────────────────
@runtime_checkable
class DeviceBase(Protocol):
    """Every adapter: identity + lifecycle + health."""
    id: str
    label: str

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    @property
    def connected(self) -> bool: ...
    @property
    def sim(self) -> bool: ...
    def status(self) -> DeviceStatus: ...


@runtime_checkable
class Streaming(Protocol):
    """Continuous data producers (daqbook, strainbook, ate loads)."""

    def start(self) -> None: ...
    def stop(self) -> None: ...
    def channels(self) -> List[ChannelSpec]: ...
    def latest(self) -> Dict[str, float]: ...
    def drain_block(self) -> Dict[str, np.ndarray]: ...
    def sample_rate(self) -> float: ...           # Hz, for wf_increment


@runtime_checkable
class Positioner(Protocol):
    """Motion devices (crescent alpha/beta, ate motion, traverse x/y/z)."""

    def move_to(self, **axes: float) -> MoveHandle: ...
    def axes(self) -> List[AxisSpec]: ...
    def positions(self) -> Dict[str, float]: ...  # current axis values
    def settled(self) -> bool: ...
    def stop_all(self) -> None: ...


@runtime_checkable
class Zeroable(Protocol):
    """Devices with a tare/zero (balances)."""

    def zero(self, seconds: float = 0.5) -> Dict[str, float]: ...


@runtime_checkable
class SetpointDevice(Protocol):
    """The tunnel: commanded setpoint + readback.

    NOTE vs the original spec: the real tunnel PLC (Red Lion → GE PLCs)
    exposes fan RPM, not Mach — Mach/q are computed in
    :mod:`freestream.derived` from the DaqBook channels. Setpoints here
    are therefore in RPM; sweeps specify per-point MACH, which
    :mod:`freestream.machloop` converts to a clamped RPM command against
    this interface (a run-sheet ``rpm`` column bypasses that loop).
    """

    def set_target(self, **kw: float) -> None: ...
    def at_target(self) -> bool: ...
    def readback(self) -> Dict[str, float]: ...


def capabilities(dev: object) -> List[str]:
    """Human-readable capability list for the rail/registry display."""
    out = []
    for proto, name in ((Streaming, "streaming"), (Positioner, "positioner"),
                        (Zeroable, "zeroable"), (SetpointDevice, "setpoint")):
        if isinstance(dev, proto):
            out.append(name)
    return out
