"""CrescentAdapter — HAL Positioner over ac_delta.CrescentDrive.

Wraps the dual Delta C2000 crescent drive (Alpha/Beta over Modbus TCP)
as a :class:`~freestream.hal.Positioner`. Axis names are the HAL
lowercase convention (``alpha``/``beta``); limits and the settled
tolerance come straight from the driver's :class:`AxisConfig`.

Constraints honoured here (see ac_delta.device):

* ``move_to`` refuses uncalibrated axes — in sim the adapter marks both
  axes calibrated before connect (the default configs already carry the
  measured slopes; only the per-setup offset zero is missing, which the
  emulator does not need).
* ``settled()`` is "no axis moving or jogging" per ``drive.state()``.
* ``stop_all()`` is the synchronous E-stop write, safe from any thread.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional

_DEVICES_DIR = Path(__file__).resolve().parents[2] / "devices"
if str(_DEVICES_DIR) not in sys.path:
    sys.path.insert(0, str(_DEVICES_DIR))

from ac_delta.config import CrescentConfig                    # noqa: E402
from ac_delta.device import CrescentDrive                     # noqa: E402

from ..hal import (AxisSpec, DeviceStatus, MoveHandle, FAULT,   # noqa: E402
                   OFFLINE, OK)
from ._configurable import ConfigurableAdapter                 # noqa: E402


class CrescentAdapter(ConfigurableAdapter):
    """Positioner adapter for the ARC Crescent alpha/beta drive."""

    id = "crescent"
    label = "ARC Crescent (alpha/beta)"
    settings_dialog_path = "ac_delta.app.settings_dialog:SettingsDialog"

    def __init__(self, sim: bool = False,
                 config_path: Optional[str] = None):
        cfg = (CrescentConfig.load(config_path) if config_path
               else CrescentConfig())
        cfg.force_sim = bool(sim)
        if sim:
            # The emulator has no calibration offset to enter; trust the
            # configured slopes so angle moves work out of the box.
            for ax in (cfg.alpha, cfg.beta):
                ax.calibrated = True
        self._cfg = cfg
        self._drive = CrescentDrive(cfg)
        self._sim = bool(sim)

    # ── ConfigurableAdapter ──────────────────────────────────────────────
    def apply_config_dict(self, data) -> None:
        """Generic apply + rebind, then re-assert the adapter invariants:
        sim/live is Freestream's switch (never a loaded ``force_sim``), and
        in sim both axes stay calibrated so angle moves keep working."""
        super().apply_config_dict(data)
        self._cfg.force_sim = self._sim
        if self._sim:
            for ax in (self._cfg.alpha, self._cfg.beta):
                ax.calibrated = True

    # ── DeviceBase ───────────────────────────────────────────────────────
    def connect(self) -> None:
        self._drive.connect()

    def disconnect(self) -> None:
        self._drive.disconnect()

    @property
    def connected(self) -> bool:
        return self._drive.connected

    @property
    def sim(self) -> bool:
        return self._sim

    def status(self) -> DeviceStatus:
        if not self._drive.connected:
            return DeviceStatus(state=OFFLINE, message="not connected",
                                sim=self._sim)
        uncal = [n for n, s in self._drive.state().items()
                 if not s["calibrated"]]
        if uncal:
            return DeviceStatus(
                state=FAULT, sim=self._sim,
                message=f"uncalibrated: {'/'.join(uncal)} — angle moves "
                        f"disabled")
        return DeviceStatus(state=OK, sim=self._sim)

    # ── Positioner ───────────────────────────────────────────────────────
    def axes(self) -> List[AxisSpec]:
        return [AxisSpec(name=ax.name.lower(), unit="deg",
                         min=ax.min_deg, max=ax.max_deg,
                         tolerance=ax.tolerance_deg)
                for ax in (self._cfg.alpha, self._cfg.beta)]

    def move_to(self, **axes: float) -> MoveHandle:
        unknown = set(axes) - {"alpha", "beta"}
        if unknown:
            raise ValueError(f"unknown axes {sorted(unknown)}; "
                             f"crescent has alpha/beta")
        self._drive.move_to(alpha=axes.get("alpha"),
                            beta=axes.get("beta"))
        return MoveHandle(targets=dict(axes))

    def positions(self) -> Dict[str, float]:
        return {name.lower(): st["angle"]
                for name, st in self._drive.state().items()}

    def settled(self) -> bool:
        return not any(st["moving"] or st["jogging"]
                       for st in self._drive.state().values())

    def stop_all(self) -> None:
        self._drive.stop_all()
