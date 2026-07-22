"""AuxSource adapter — feeds tunnel q (and temperature) to other apps.

The ATE balance app polls a small duck-typed interface
(``dynamic_pressure() -> Pa | None``, ``temperature_k() -> K | None``; see
``ate_balance/aux.py``).  This adapter serves it straight from a running
:class:`~daqbook_2000.device.Daqbook2000` ring buffer, so wiring the real
DaqBook into the balance app is::

    from daqbook_2000 import Daqbook2000, DaqbookConfig, DaqbookAuxSource
    dev = Daqbook2000(DaqbookConfig())
    dev.connect(); dev.start()
    panel.aux = DaqbookAuxSource(dev)          # ate_balance panel

No import of ate_balance here — the package stays standalone.
"""

from __future__ import annotations

from typing import Optional

PSI_TO_PA = 6894.75729


class DaqbookAuxSource:
    """Latest tunnel conditions from a live Daqbook2000 stream."""

    def __init__(self, device, *,
                 q_channel: str = "Pdiff",
                 t_channel: str = "Temp",
                 q_to_pa: float = PSI_TO_PA,
                 avg_scans: int = 100,
                 own_device: bool = False):
        self._dev = device
        self._q_channel = q_channel
        self._t_channel = t_channel
        self._q_to_pa = q_to_pa
        self._avg = max(1, avg_scans)
        self._own_device = own_device

    def _mean(self, field: str) -> Optional[float]:
        ring = getattr(self._dev, "ring", None)
        if ring is None or field not in ring.fields:
            return None
        tail = ring.tail(self._avg)
        vals = tail[field]
        if vals.size == 0:
            return None
        return float(vals.mean())

    def dynamic_pressure(self) -> Optional[float]:
        """Mean q over the last ``avg_scans``, converted to Pa."""
        q = self._mean(self._q_channel)
        return None if q is None else q * self._q_to_pa

    def temperature_k(self) -> Optional[float]:
        """Temperature channel in K if its unit is degC/degF/K, else None."""
        ring = getattr(self._dev, "ring", None)
        if ring is None:
            return None
        t = self._mean(self._t_channel)
        if t is None:
            return None
        unit = ""
        for ch in getattr(self._dev.config, "channels", []):
            if ch.name == self._t_channel:
                unit = ch.unit.lower()
                break
        if unit in ("degc", "c", "°c"):
            return t + 273.15
        if unit in ("degf", "f", "°f"):
            return (t - 32.0) * 5.0 / 9.0 + 273.15
        if unit in ("k", "kelvin"):
            return t
        return None            # raw volts — no honest conversion available

    def close(self) -> None:
        """Disconnect the device iff this adapter was told it owns it."""
        if self._own_device:
            self._dev.disconnect()
