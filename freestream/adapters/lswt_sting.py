"""LswtStingAdapter — HAL Positioner over lswt_sting.StingDrive.

Wraps the dual serial stepper-indexer sting drive (Alpha/Beta on one
RS-232 daisy chain) as a :class:`~freestream.hal.Positioner`. Axis names
are the HAL lowercase convention (``alpha``/``beta``); limits and the
settled tolerance come straight from the driver's
:class:`StingAxisConfig`.

Constraints honoured here (see lswt_sting.device):

* ``move_to`` refuses un-zeroed axes — the sting is open loop (indexer
  step counter, no encoder), so the operator must declare the physical
  angle first (:meth:`set_current_angle`). In sim the adapter marks both
  axes zeroed before connect (the emulator's counter starts at zero, so
  the default ``zero_offset_deg`` reference is already true).
* A latched drive FAULT (stall ``*S``, move timeout, serial watchdog) is
  surfaced as a FAULT status and blocks all motion — the driver raises
  ``RuntimeError`` until ``reset_fault()``.
* ``settled()`` is "no axis moving" per ``drive.state()``.
* ``stop_all()`` is the synchronous E-stop write, safe from any thread.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional

_DEVICES_DIR = Path(__file__).resolve().parents[2] / "devices"
if str(_DEVICES_DIR) not in sys.path:
    sys.path.insert(0, str(_DEVICES_DIR))

from lswt_sting.config import StingConfig, load_startup_config  # noqa: E402
from lswt_sting.device import StingDrive                      # noqa: E402

from ..hal import (AxisSpec, DeviceStatus, MoveHandle, FAULT,   # noqa: E402
                   OFFLINE, OK)
from ._configurable import ConfigurableAdapter                 # noqa: E402


class LswtStingAdapter(ConfigurableAdapter):
    """Positioner adapter for the LSWT sting alpha/beta drive."""

    id = "lswt_sting"
    label = "LSWT Sting (alpha/beta)"
    settings_dialog_path = "lswt_sting.app.settings_dialog:SettingsDialog"
    comscan_module = "lswt_sting.comscan"
    comscan_hit_attr = "is_sting"

    def __init__(self, sim: bool = False,
                 config_path: Optional[str] = None):
        # Config provenance mirrors the standalone app: an explicit path
        # wins; otherwise a LIVE session starts from the device's own
        # startup defaults (defaults_path()) — the rig-proven COM port
        # and limits — while SIM stays on hermetic factory defaults
        # (deterministic tests/demo, no dependence on the developer's
        # home directory).
        if config_path:
            cfg = StingConfig.load(config_path)
        elif sim:
            cfg = StingConfig()
        else:
            cfg = load_startup_config()
        cfg.force_sim = bool(sim)
        if sim:
            # The emulator's step counter starts at zero, so the default
            # zero reference is already true; trust it so absolute angle
            # moves work out of the box.
            for ax in (cfg.alpha, cfg.beta):
                ax.zeroed = True
        self._cfg = cfg
        self._dev = StingDrive(cfg)
        self._sim = bool(sim)

    # ── ConfigurableAdapter ──────────────────────────────────────────────
    def apply_config_dict(self, data) -> None:
        """Generic apply + rebind, then re-assert the adapter invariants:
        sim/live is Freestream's switch (never a loaded ``force_sim``), and
        in sim both axes stay zeroed so angle moves keep working."""
        super().apply_config_dict(data)
        self._cfg.force_sim = self._sim
        if self._sim:
            for ax in (self._cfg.alpha, self._cfg.beta):
                ax.zeroed = True

    # ── DeviceBase ───────────────────────────────────────────────────────
    def connect(self) -> None:
        """Connect like the standalone flow: resolve the COM port first
        (blank port → one comscan), and if the configured port does not
        answer, one rescue scan before giving up — the embedded
        equivalent of the operator's Search button."""
        self.resolve_com_port()
        try:
            self._dev.connect()
        except Exception:
            if not self.rescue_com_port():
                raise
            self._dev.connect()

    def disconnect(self) -> None:
        self._dev.disconnect()

    @property
    def connected(self) -> bool:
        return self._dev.connected

    @property
    def sim(self) -> bool:
        return self._sim

    def status(self) -> DeviceStatus:
        if not self._dev.connected:
            return DeviceStatus(state=OFFLINE, message="not connected",
                                sim=self._sim)
        if self._dev.fault:
            return DeviceStatus(state=FAULT, sim=self._sim,
                                message=self._dev.fault)
        unzeroed = [n for n, s in self._axis_states().items()
                    if s["enabled"] and not s["zeroed"]]
        if unzeroed:
            return DeviceStatus(
                state=FAULT, sim=self._sim,
                message=f"not zeroed: {'/'.join(unzeroed)} — absolute "
                        f"moves disabled (jog only)")
        return DeviceStatus(state=OK, sim=self._sim)

    # ── Positioner ───────────────────────────────────────────────────────
    def axes(self) -> List[AxisSpec]:
        return [AxisSpec(name=ax.name.lower(), unit="deg",
                         min=ax.min_deg, max=ax.max_deg,
                         tolerance=ax.tolerance_deg)
                for ax in self._cfg.enabled_axes()]

    def move_to(self, **axes: float) -> MoveHandle:
        unknown = set(axes) - {"alpha", "beta"}
        if unknown:
            raise ValueError(f"unknown axes {sorted(unknown)}; "
                             f"lswt_sting has alpha/beta")
        self._dev.move_to(alpha=axes.get("alpha"),
                          beta=axes.get("beta"))
        return MoveHandle(targets=dict(axes))

    def positions(self) -> Dict[str, float]:
        return {name.lower(): st["angle"]
                for name, st in self._axis_states().items()}

    def settled(self) -> bool:
        return not any(st["moving"] for st in self._axis_states().values())

    def stop_all(self) -> None:
        self._dev.stop_all()

    # ── zero reference ───────────────────────────────────────────────────
    def set_current_angle(self, name: str, angle: float) -> None:
        """Declare the sting's PHYSICAL angle and zero the axis counter
        (the open-loop analogue of the crescent's offset calibration)."""
        self._dev.set_current_angle(name, angle)

    def reset_fault(self) -> None:
        """Clear a latched drive fault (after the cause is addressed)."""
        self._dev.reset_fault()

    # ── helpers ──────────────────────────────────────────────────────────
    def _axis_states(self) -> Dict[str, dict]:
        """Per-axis entries of ``drive.state()`` (which also carries a
        top-level ``"fault"`` string alongside the axis dicts)."""
        return {name: st for name, st in self._dev.state().items()
                if isinstance(st, dict)}
