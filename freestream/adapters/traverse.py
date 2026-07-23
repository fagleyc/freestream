"""TraverseAdapter — HAL Positioner over traverse_swt.TraverseDrive.

Wraps the SSWT 3-axis WAGO traverse (X axial, Y lateral, Z vertical)
as a Positioner with lowercase HAL axis names ``x``/``y``/``z`` in
inches. Limits and the settled tolerance come from the driver's
:class:`AxisConfig`.

Constraints honoured here (see traverse_swt.device/config):

* ``move_to`` refuses uncalibrated axes. The 750-673 counter zeroes at
  PLC power-up so real axes start uncalibrated; in sim the adapter
  marks all axes calibrated before connect (the signed slopes — e.g.
  Y −14841 clicks/inch — plus counts_high=0 / inch_high=0 defaults
  give a plausible zero-referenced rig).
* ``settled()`` is "no axis moving" per ``drive.state()`` (stall aborts
  and wrong-way trips clear that flag, so a blocked move reports settled
  rather than hanging a sweep — check positions()).
* ``stop_all()`` writes ControlWord 0 synchronously (E-stop path).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional

_DEVICES_DIR = Path(__file__).resolve().parents[2] / "devices"
if str(_DEVICES_DIR) not in sys.path:
    sys.path.insert(0, str(_DEVICES_DIR))

from traverse_swt.config import (TraverseConfig,              # noqa: E402
                                 load_startup_config)
from traverse_swt.device import TraverseDrive                 # noqa: E402

from ..hal import (AxisSpec, DeviceStatus, MoveHandle, FAULT,  # noqa: E402
                   OFFLINE, OK)
from ._configurable import ConfigurableAdapter                 # noqa: E402


class TraverseAdapter(ConfigurableAdapter):
    """Positioner adapter for the SSWT x/y/z traverse."""

    id = "traverse"
    label = "SSWT traverse (x/y/z)"
    settings_dialog_path = "traverse_swt.app.settings_dialog:SettingsDialog"

    def __init__(self, sim: bool = False,
                 config_path: Optional[str] = None):
        # Config provenance mirrors the standalone app: an explicit path
        # wins; otherwise a LIVE session starts from the device's own
        # startup defaults (defaults_path() — the operator's rig-proven
        # "Set as Defaults" calibration/limits), while SIM stays on
        # hermetic factory defaults (deterministic tests/demo).
        if config_path:
            cfg = TraverseConfig.load(config_path)
        elif sim:
            cfg = TraverseConfig()
        else:
            cfg = load_startup_config()
        cfg.force_sim = bool(sim)
        if sim:
            # No power-cycle offset to re-zero in the emulator: trust
            # the configured slopes so position moves work directly.
            # Zero the offset at min_in (the homing datum for Y/Z), so
            # every in-range target moves AWAY from the emulator's
            # negative-end limit switch — mirrors a freshly homed rig.
            for ax in cfg.axes():
                ax.calibrated = True
                ax.inch_high = ax.min_in
                ax.counts_high = 0
        self._cfg = cfg
        self._drive = TraverseDrive(cfg)
        self._sim = bool(sim)

    # ── DeviceBase ───────────────────────────────────────────────────────
    def connect(self) -> None:
        self._drive.connect()
        if self._sim and hasattr(self._drive._plc, "sim_rate"):
            # fast sim plant — the emulator's fixed rate is the
            # rig-realistic 2000 counts/s; sweep moves would crawl
            self._drive._plc.sim_rate = 25_000.0

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
        uncal = [n for n, s in state.items()
                 if s["enabled"] and not s["calibrated"]]
        if uncal:
            return DeviceStatus(
                state=FAULT, sim=self._sim,
                message=f"uncalibrated: {'/'.join(uncal)} — position "
                        f"moves disabled")
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
        return {name.lower(): st["inches"]
                for name, st in self._drive.state().items()
                if st["enabled"]}

    def settled(self) -> bool:
        return not any(st["moving"]
                       for st in self._drive.state().values())

    def stop_all(self) -> None:
        self._drive.stop_all()
