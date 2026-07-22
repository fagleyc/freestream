"""TunnelAdapter — HAL SetpointDevice over tunnel_plc (fan RPM).

The real tunnel PLC path (Red Lion G315 → GE PLCs) exposes fan RPM, not
Mach — Mach/q are computed elsewhere from the DaqBook channels, so the
setpoint here is RPM.

Read/write separation preserved from the driver:

* Reads go through :class:`tunnel_plc.monitor.TunnelMonitor` (poll
  thread, reconnect/backoff, ``stale`` flag).
* Writes go ONLY through :class:`tunnel_plc.control.TunnelControl`,
  which demands ``enable_writes=True`` and refuses RPM commands while
  ``config.rpm_max`` is 0 (not configured). The adapter constructs it
  LAZILY inside :meth:`set_target`, and only when ``rpm_max > 0`` —
  connecting/monitoring never arms the write path. In sim the config
  gets ``rpm_max = 1000`` so sweeps work out of the box.
* The sim plant only spools toward RPM_Set while the fan runs, so in
  sim :meth:`set_target` also pulses the fan-start button when needed.
  On hardware starting the fan stays a deliberate operator action.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Optional

_DEVICES_DIR = Path(__file__).resolve().parents[3] / "devices"
if str(_DEVICES_DIR) not in sys.path:
    sys.path.insert(0, str(_DEVICES_DIR))

from tunnel_plc.config import TunnelConfig                    # noqa: E402
from tunnel_plc.monitor import TunnelMonitor                  # noqa: E402

from ..hal import DeviceStatus, FAULT, OFFLINE, OK            # noqa: E402


class TunnelAdapter:
    """SetpointDevice adapter for the SSWT tunnel fan RPM."""

    id = "tunnel"
    label = "Tunnel PLC (fan RPM)"

    def __init__(self, sim: bool = False,
                 config_path: Optional[str] = None,
                 rpm_tol: float = 25.0,
                 rpm_max: Optional[float] = None):
        cfg = (TunnelConfig.load(config_path) if config_path
               else TunnelConfig())
        cfg.force_sim = bool(sim)
        if sim:
            cfg.rpm_max = 1000.0          # arm sim writes out of the box
        elif rpm_max is not None:
            cfg.rpm_max = float(rpm_max)
        self._cfg = cfg
        self._monitor = TunnelMonitor(cfg)
        self._control = None              # built lazily on first write
        self._sim = bool(sim)
        self._rpm_tol = float(rpm_tol)
        self._target: Optional[float] = None

    # ── DeviceBase ───────────────────────────────────────────────────────
    def connect(self) -> None:
        self._monitor.connect()

    def disconnect(self) -> None:
        self._monitor.disconnect()
        self._control = None

    @property
    def connected(self) -> bool:
        return self._monitor.running

    @property
    def sim(self) -> bool:
        return self._sim

    def status(self) -> DeviceStatus:
        if not self._monitor.running:
            return DeviceStatus(state=OFFLINE, message="not connected",
                                sim=self._sim)
        snap = self._monitor.snapshot()
        age = None if snap.age_s == float("inf") else snap.age_s
        if snap.inverter_fault:
            return DeviceStatus(state=FAULT, sim=self._sim,
                                message="Inverter_Fault_Light set",
                                last_sample_age_s=age)
        if snap.stale:
            return DeviceStatus(state=OFFLINE, sim=self._sim,
                                message=f"snapshot stale "
                                        f"({snap.age_s:.1f}s)",
                                last_sample_age_s=age)
        return DeviceStatus(state=OK, sim=self._sim,
                            last_sample_age_s=age)

    # ── SetpointDevice ───────────────────────────────────────────────────
    def _get_control(self):
        """Build the guarded write path on first use (rpm_max > 0 only)."""
        if self._control is None:
            if self._cfg.rpm_max <= 0:
                raise RuntimeError(
                    "tunnel writes disabled: config.rpm_max is 0 (not "
                    "configured) — set a real limit before commanding "
                    "speed")
            from tunnel_plc.control import TunnelControl
            self._control = TunnelControl(self._cfg, self._monitor,
                                          enable_writes=True)
        return self._control

    def set_target(self, **kw: float) -> None:
        if "rpm" not in kw:
            raise ValueError(f"tunnel setpoint is rpm=<value>; "
                             f"got {sorted(kw)}")
        control = self._get_control()
        sent = control.set_rpm(float(kw["rpm"]))
        self._target = sent
        if self._sim and sent > 0 and \
                not self._monitor.snapshot().fan_running:
            control.start_tunnel_fan()    # sim only spools while running

    def at_target(self) -> bool:
        snap = self._monitor.snapshot()
        if snap.stale:
            return False
        target = self._target if self._target is not None \
            else snap.rpm_set
        return abs(snap.actual_rpm - target) <= self._rpm_tol

    def readback(self) -> Dict[str, float]:
        snap = self._monitor.snapshot()
        return {"rpm": snap.actual_rpm, "rpm_set": snap.rpm_set}
