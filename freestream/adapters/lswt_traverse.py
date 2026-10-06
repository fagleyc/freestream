"""LswtTraverseAdapter — HAL Positioner over lswt_traverse.LswtTraverseDrive.

Wraps the South LSWT 3-axis traverse (three IDC SmartStep23 SmartDrives
on one RS-232C daisy chain; X axial, Y lateral, Z vertical) as a
Positioner with lowercase HAL axis names ``x``/``y``/``z`` in inches.
Limits and the settled tolerance come from the driver's
:class:`AxisConfig`.

Constraints honoured here (see lswt_traverse.device/config):

* There is NO counts calibration and NO homing routine on this rig —
  the referencing analogue is ``state()["referenced"]``: the operator
  jogs each axis to its reference spot and presses "Set home here"
  (``set_home``), which arms the host-side soft travel limits. A
  SmartStep wakes up reading 0.000 wherever it stands, so ``move_to``
  REFUSES un-referenced axes (driver-enforced) and :meth:`status`
  reports FAULT until every enabled axis is referenced. In sim the
  adapter references all axes at connect (``set_home_all``) so sweeps
  run out of the box.
* Per-axis positions come from ``state()``'s ``"position"`` key
  (inches — this driver has no ``"inches"`` key, unlike the SWT WAGO
  traverse).
* ``settled()`` is "no axis moving" per ``drive.state()`` (a
  hardware-limit latch or move timeout clears the target and the moving
  flag, so a blocked move reports settled rather than hanging a sweep —
  check positions()).
* ``stop_all()`` broadcasts a decel-stop to the whole chain
  synchronously (the E-stop path).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional

_DEVICES_DIR = Path(__file__).resolve().parents[2] / "devices"
if str(_DEVICES_DIR) not in sys.path:
    sys.path.insert(0, str(_DEVICES_DIR))

from lswt_traverse.config import TraverseConfig               # noqa: E402
from lswt_traverse.device import LswtTraverseDrive            # noqa: E402

from ..hal import (AxisSpec, DeviceStatus, MoveHandle, FAULT,  # noqa: E402
                   OFFLINE, OK)
from ._configurable import ConfigurableAdapter                 # noqa: E402


class LswtTraverseAdapter(ConfigurableAdapter):
    """Positioner adapter for the South LSWT x/y/z traverse."""

    id = "lswt_traverse"
    label = "South LSWT traverse (x/y/z)"
    #: the device app has no SettingsDialog — configuration happens in
    #: Freestream's generic DeviceConfigDialog (Settings + axis tabs)
    settings_dialog_path = ""

    def __init__(self, sim: bool = False,
                 config_path: Optional[str] = None):
        # Config provenance mirrors the standalone app: an explicit path
        # wins; otherwise a LIVE session starts from the device's own
        # startup defaults (TraverseConfig.load_defaults() — the
        # operator's rig-proven "Set as Defaults" COM port / travel
        # limits; never raises), while SIM stays on hermetic factory
        # defaults (deterministic tests/demo).
        if config_path:
            cfg = TraverseConfig.load(config_path)
        elif sim:
            cfg = TraverseConfig()
        else:
            cfg = TraverseConfig.load_defaults()
        cfg.force_sim = bool(sim)
        if sim:
            # fast sim plant — the SimChain integrates at the commanded
            # VE, which the driver sends as velocity_ips (rig-realistic
            # 1 in/s); sweep moves across the ±12" span would crawl.
            for ax in cfg.axes():
                ax.velocity_ips = max(ax.velocity_ips, 25.0)
        self._cfg = cfg
        self._drive = LswtTraverseDrive(cfg)
        self._sim = bool(sim)

    # ── DeviceBase ───────────────────────────────────────────────────────
    def connect(self) -> None:
        self._drive.connect()
        if self._sim:
            # No operator to jog-and-set-home in the emulator: reference
            # every axis at its 0.000 wake-up spot so absolute moves (and
            # therefore sweeps) work out of the box — record_blockers
            # stays empty in sim. On hardware referencing remains a
            # deliberate operator action in the embedded device tabs.
            self._drive.set_home_all()

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
        state = self._drive.state()
        unref = [n for n, s in state.items()
                 if s["enabled"] and not s["referenced"]]
        if unref:
            return DeviceStatus(
                state=FAULT, sim=self._sim,
                message=f"not referenced: {'/'.join(unref)} — jog to the "
                        f"reference spot and Set home; position moves "
                        f"disabled")
        return DeviceStatus(state=OK, sim=self._sim, message="")

    # ── Positioner ───────────────────────────────────────────────────────
    def axes(self) -> List[AxisSpec]:
        return [AxisSpec(name=ax.name.lower(), unit="in",
                         min=ax.min_in, max=ax.max_in,
                         tolerance=ax.tolerance_in)
                for ax in self._cfg.axes() if ax.enabled]

    def move_to(self, **axes: float) -> MoveHandle:
        unknown = set(axes) - {"x", "y", "z"}
        if unknown:
            raise ValueError(f"unknown axes {sorted(unknown)}; "
                             f"traverse has x/y/z")
        self._drive.move_to(x=axes.get("x"), y=axes.get("y"),
                            z=axes.get("z"))
        return MoveHandle(targets=dict(axes))

    def positions(self) -> Dict[str, float]:
        return {name.lower(): st["position"]
                for name, st in self._drive.state().items()
                if st["enabled"]}

    def settled(self) -> bool:
        return not any(st["moving"]
                       for st in self._drive.state().values())

    def stop_all(self) -> None:
        self._drive.stop_all()
