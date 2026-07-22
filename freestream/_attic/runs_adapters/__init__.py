"""Thin adapters wrapping the projects/devices drivers to the HAL.

One module per device package; each implements the capability
Protocols in :mod:`freestream.hal` WITHOUT modifying the driver:

=============  ==========================  ============================
Module         Driver package              Capabilities
=============  ==========================  ============================
crescent       ac_delta.CrescentDrive      Positioner (alpha/beta, deg)
strainbook     strainbook_616              Streaming + Zeroable
daqbook        daqbook_2000                Streaming (tunnel conditions)
ate            ate_balance                 Streaming + Positioner +
                                           Zeroable (Mode 2 all-in-one)
tunnel         tunnel_plc                  SetpointDevice (fan RPM)
traverse       traverse_swt.TraverseDrive  Positioner (x/y/z, inches)
=============  ==========================  ============================

Every adapter is constructed by the DeviceManager as
``cls(sim=bool, **options)`` and then gets its ``id`` assigned; each
module carries its own sys.path guard so it also imports standalone.
The recorder owns time — adapters never emit a "Time" channel.
"""

from __future__ import annotations

__all__ = ["crescent", "strainbook", "daqbook", "ate", "tunnel",
           "traverse"]
