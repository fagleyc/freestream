"""Freestream — wind-tunnel test orchestration & raw acquisition suite.

The tunnel-side counterpart to Streamlined: commands alpha/beta (and
tunnel RPM) sweeps over the existing device drivers, streams live
engineering-unit data, and records RAW per-test-point HDF5 files that
Streamlined reduces. Calibration is never applied at capture time.

Layers: :mod:`freestream.hal` (capability contracts) →
:mod:`freestream.adapters` (thin wrappers over projects/devices drivers) →
:mod:`freestream.manager` (registry/modes) → :mod:`freestream.sweep`
(state-machine engine) → :mod:`freestream.recorder` (HDF5) →
:mod:`freestream.app` (dock GUI).
"""

from __future__ import annotations

__version__ = "0.1.0"

from .hal import (AxisSpec, ChannelSpec, DeviceStatus, MoveHandle,
                  capabilities)
from .manager import DeviceManager

__all__ = ["AxisSpec", "ChannelSpec", "DeviceStatus", "MoveHandle",
           "capabilities", "DeviceManager", "__version__"]
