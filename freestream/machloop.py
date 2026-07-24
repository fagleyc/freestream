"""MachLoop — tunnel targeting strategy: target Mach → fan-RPM command.

The tunnel PLC (Red Lion → GE PLCs) exposes fan RPM, not Mach, and the
tunnel_plc driver stays raw/standalone by project rule — so Mach lives
HERE, in the Freestream composition layer, never in the driver:

* target Mach → RPM via ``config.rpm_per_mach`` (a rig-tuned linear map),
  always clamped to ``[0, rpm_max]`` from the tunnel adapter's own config;
* the RPM command goes through the ordinary HAL
  :class:`~freestream.hal.SetpointDevice` (``set_target(rpm=...)``);
* *at target* is decided from the MEASURED Mach — the isentropic chain in
  :mod:`freestream.derived` over the tunnel-condition channels
  (Pdiff/Ptot/Temp engineering units via ``latest()``), found BY NAME
  across the registry's streaming devices (all three on the SWT DaqBook,
  or split across devices as in the LSWT mode) — within
  ``config.mach_tolerance``, AND the RPM readback settled;
* in SIM the sim DAQ pressures do not respond to fan RPM, so the loop
  falls back to the RPM-proxy ``at_target`` with a clear log line
  ("sim: Mach loop proxied by RPM").

Correction policy (v1, NO runaway loops): command → wait for RPM settle →
check measured Mach. If live and off by more than the tolerance, adjust
the RPM proportionally (``rpm * mach_target / mach_meas``) and retry, up
to ``config.mach_max_iterations`` commands total; then raise (the sweep
point FAULTs with a clear message). Every command is clamped.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

from . import speed
from .config import FreestreamConfig
from .derived import (TUNNEL_CONDITION_CHANNELS, live_tunnel_state,
                      tunnel_condition_sources, tunnel_state)
from .hal import SetpointDevice, Streaming

log = logging.getLogger(__name__)


def _safe_readback(dev) -> dict:
    try:
        return dict(dev.readback() or {})
    except Exception:                                  # noqa: BLE001
        return {}


def _adapter_limit(dev, *keys) -> float:
    """The tunnel adapter's own write ceiling (0 = not configured): the
    first present of *keys* on ``dev.config`` (``max_hz`` for the LSWT
    drive, ``rpm_max`` for the SWT PLC)."""
    cfg = getattr(dev, "config", None) or getattr(dev, "_cfg", None)
    for key in keys:
        try:
            val = float(getattr(cfg, key, 0.0) or 0.0)
        except (TypeError, ValueError):
            val = 0.0
        if val > 0:
            return val
    return 0.0


def command_kwarg_for(dev, target_mach, entered_value, unit, config):
    """Pick the adapter's NATIVE command (kwarg, value) for a speed target.

    The whole point of the 3-tier rework: never force everything through
    Mach→RPM. Instead command the drive in its OWN parameter, honoring the
    operator's selected unit:

    * LSWT-style drive (readback carries ``hz`` / ``velocity_fps``): prefer
      ``hz=`` when the unit is Hz or a mach-derived Hz (also RPM, which is
      1:1 with Hz on this drive); ``velocity=`` (ft/s) for the velocity
      units.
    * SWT-style drive (readback carries ``rpm``): ``rpm=`` — the entered
      RPM verbatim when the unit is RPM, else the nominal Mach→RPM map.

    The ENTERED value in native units is authoritative when the unit
    matches the drive parameter; otherwise the canonical Mach is converted
    through :mod:`freestream.speed` (calibration for Hz, standard-day A0
    for velocities, ``rpm_per_mach`` for RPM)."""
    rb = _safe_readback(dev)
    mach = float(target_mach)
    rpm_per_mach = float(getattr(config, "rpm_per_mach", 1500.0))
    lswt = ("hz" in rb) or ("velocity_fps" in rb)
    if lswt:
        if unit in ("ft/s", "m/s") and "velocity_fps" in rb:
            if entered_value is not None:
                fps = (speed.convert_velocity_ms(float(entered_value), "ft/s")
                       if unit == "m/s" else float(entered_value))
            else:
                fps = speed.value_from_mach(mach, "ft/s", rpm_per_mach)
            return "velocity", fps
        # hz / rpm / mach → the drive's output frequency (hz)
        if unit in ("hz", "rpm") and entered_value is not None:
            hz = float(entered_value)          # native drive Hz (rpm≡hz 1:1)
        else:
            hz = speed.value_from_mach(mach, "hz", rpm_per_mach)
        return "hz", hz
    # SWT-style: fan RPM
    if unit == "rpm" and entered_value is not None:
        rpm = float(entered_value)
    else:
        rpm = speed.value_from_mach(mach, "rpm", rpm_per_mach)
    return "rpm", rpm


def clamp_command(dev, kwarg, value):
    """Clamp a native command to [0, adapter-limit] (max_hz for Hz, rpm_max
    for RPM; velocity is only floored at 0 — the drive clamps its own Hz)."""
    value = max(float(value), 0.0)
    if kwarg == "hz":
        limit = _adapter_limit(dev, "max_hz")
    elif kwarg == "rpm":
        limit = _adapter_limit(dev, "rpm_max")
    else:
        limit = 0.0
    if limit > 0 and value > limit:
        return limit
    return value

#: WaitFn(condition, timeout_s, fail_msg) — the sweep engine's abortable
#: _wait; tests may pass a simple polling loop.
WaitFn = Callable[[Callable[[], bool], float, str], None]


def find_tunnel_daq(streams) -> Optional[Streaming]:
    """The single streaming device carrying ALL of Pdiff/Ptot/Temp (the
    classic one-DAQ fast path: the SWT DaqBook), if present. Cross-device
    splits (LSWT: NI + Heise) have no such device — use the registry
    helpers (:func:`registry_mach`, ``derived.live_tunnel_state``)."""
    need = set(TUNNEL_CONDITION_CHANNELS)
    for s in streams:
        try:
            if need <= {ch.name for ch in s.channels()}:
                return s
        except Exception:                              # noqa: BLE001
            continue
    return None


def daq_mach(daq: Optional[Streaming]) -> Optional[float]:
    """Isentropic Mach from ONE stream's ``latest()`` (all three tunnel
    channels on the same device). None when unavailable/invalid."""
    if daq is None:
        return None
    try:
        vals = daq.latest()
    except Exception:                                  # noqa: BLE001
        return None
    if not all(k in vals for k in TUNNEL_CONDITION_CHANNELS):
        return None
    st = tunnel_state(float(vals["Pdiff"]), float(vals["Ptot"]),
                      float(vals["Temp"]))
    return st.mach if st.valid else None


def registry_mach(manager) -> Optional[float]:
    """Isentropic Mach from the REGISTRY: Pdiff/Ptot/Temp found by
    channel name across all streaming devices (cross-device in the LSWT
    mode; the SWT DaqBook keeps its one-``latest()`` fast path). The ONE
    measurement shared by MachLoop closure and the sweep engine's
    monitor-only operator wait. None when unavailable/invalid."""
    st = live_tunnel_state(manager)
    return st.mach if st is not None and st.valid else None


def make_tunnel_measure(manager,
                        setpoint: Optional[SetpointDevice]
                        ) -> Callable[[], Tuple[float, float]]:
    """Build the ``() -> (measured_mach, rpm_meas)`` probe used by the
    monitor-only operator wait (sweep.OperatorWaitRequest.measure):
    isentropic Mach from the registry's tunnel-condition channels (NaN
    when unavailable) + fan RPM from the setpoint readback — reads only,
    never a command."""
    def measure() -> Tuple[float, float]:
        mach = registry_mach(manager)
        rpm = 0.0
        if setpoint is not None:
            try:
                rpm = float(setpoint.readback().get("rpm", 0.0))
            except Exception:                          # noqa: BLE001
                rpm = 0.0
        return (mach if mach is not None else math.nan, rpm)
    return measure


@dataclass
class MachLoopResult:
    """What the loop actually did, for /Tunnel recording + logs."""
    mach_target: float
    rpm_cmd: float                   # final commanded RPM
    mach_meas: Optional[float] = None
    iterations: int = 1
    proxied: bool = False            # True = sim RPM-proxy fallback


class MachLoop:
    """Owns the Mach→RPM conversion and the at-target decision."""

    def __init__(self, setpoint: SetpointDevice, config: FreestreamConfig,
                 daq: Optional[Streaming] = None, manager=None,
                 event: Optional[Callable[[str], None]] = None):
        """``daq``: a single stream carrying all three tunnel channels
        (classic path, still honored). ``manager``: the DeviceManager —
        preferred; measurement finds Pdiff/Ptot/Temp by channel name
        across the registry (cross-device in the LSWT mode)."""
        self.setpoint = setpoint
        self.config = config
        self.daq = daq
        self.manager = manager
        self._event = event or (lambda msg: log.info(msg))

    # ── conversions / measurements ───────────────────────────────────────
    @property
    def rpm_max(self) -> float:
        """The tunnel adapter's own write limit (0 = not configured)."""
        cfg = getattr(self.setpoint, "config", None)
        try:
            return float(getattr(cfg, "rpm_max", 0.0) or 0.0)
        except (TypeError, ValueError):
            return 0.0

    def rpm_for(self, mach: float) -> float:
        """Target Mach → clamped RPM command (config.rpm_per_mach map)."""
        rpm = float(mach) * float(self.config.rpm_per_mach)
        rpm = max(rpm, 0.0)
        limit = self.rpm_max
        if limit > 0 and rpm > limit:
            self._event(f"mach loop: rpm {rpm:g} clamped to adapter "
                        f"rpm_max {limit:g}")
            rpm = limit
        return rpm

    def measured_mach(self) -> Optional[float]:
        """Isentropic Mach from the tunnel-condition channels (None if
        unavailable): the explicit single ``daq`` when given, else the
        registry-wide by-name lookup."""
        if self.daq is not None:
            return daq_mach(self.daq)
        if self.manager is not None:
            return registry_mach(self.manager)
        return None

    def _measure_devices(self):
        """The devices the measured Mach actually comes from."""
        if self.daq is not None:
            return [self.daq]
        if self.manager is not None:
            sources = tunnel_condition_sources(self.manager)
            if all(k in sources for k in TUNNEL_CONDITION_CHANNELS):
                return list({id(d): d for d in sources.values()}.values())
        return []

    @property
    def live(self) -> bool:
        """True when measured-Mach closure is possible (hardware DAQ(s))."""
        devs = self._measure_devices()
        if not devs or any(getattr(d, "sim", True) for d in devs):
            return False
        return not getattr(self.setpoint, "sim", True)

    # ── the loop ─────────────────────────────────────────────────────────
    def run(self, mach: float, wait: WaitFn) -> MachLoopResult:
        """Drive the tunnel to *mach*; returns what was commanded/measured.

        *wait* is the engine's abortable ``_wait(cond, timeout_s, msg)``.
        Raises on timeout or when the measured Mach never lands inside
        ``config.mach_tolerance`` within ``config.mach_max_iterations``
        commands (→ the sweep point FAULTs).
        """
        tol = float(self.config.mach_tolerance)
        max_iter = max(int(self.config.mach_max_iterations), 1)
        timeout = float(self.config.tunnel_timeout_s)
        rpm = self.rpm_for(mach)

        if not self.live:
            # sim plant: the sim DAQ pressures don't respond to fan RPM
            self._event(f"sim: Mach loop proxied by RPM "
                        f"(Mach {mach:g} → {rpm:g} RPM)")
            self.setpoint.set_target(rpm=rpm)
            wait(self.setpoint.at_target, timeout,
                 f"tunnel never settled at {rpm:g} RPM "
                 f"(Mach {mach:g} proxy)")
            return MachLoopResult(mach_target=mach, rpm_cmd=rpm,
                                  mach_meas=self.measured_mach(),
                                  proxied=True)

        meas: Optional[float] = None
        for i in range(1, max_iter + 1):
            self._event(f"mach loop: Mach {mach:g} → {rpm:g} RPM "
                        f"(iteration {i}/{max_iter})")
            self.setpoint.set_target(rpm=rpm)
            wait(self.setpoint.at_target, timeout,
                 f"tunnel never settled at {rpm:g} RPM "
                 f"(Mach {mach:g}, iteration {i})")
            meas = self.measured_mach()
            if meas is None:
                raise RuntimeError(
                    "mach loop: no measured Mach available from the "
                    "tunnel-condition streams — cannot confirm tunnel "
                    "condition")
            if abs(meas - mach) <= tol:
                self._event(f"mach loop: at target — measured Mach "
                            f"{meas:.3f} (target {mach:g} ± {tol:g})")
                return MachLoopResult(mach_target=mach, rpm_cmd=rpm,
                                      mach_meas=meas, iterations=i)
            if i < max_iter:
                # one proportional correction step per iteration; clamped
                rpm = self._corrected_rpm(rpm, mach, meas)
        raise RuntimeError(
            f"mach loop FAULT: measured Mach {meas:.3f} not within "
            f"±{tol:g} of target {mach:g} after {max_iter} RPM "
            f"command(s) — check rpm_per_mach tuning and tunnel health")

    def _corrected_rpm(self, rpm: float, mach: float, meas: float) -> float:
        """Proportional correction, clamped to the adapter's limits."""
        if meas <= 1e-6:                # flow not established: step up 25 %
            corrected = rpm * 1.25 if rpm > 0 else \
                float(mach) * float(self.config.rpm_per_mach)
        else:
            corrected = rpm * (float(mach) / float(meas))
        corrected = max(corrected, 0.0)
        limit = self.rpm_max
        if limit > 0 and corrected > limit:
            self._event(f"mach loop: corrected rpm {corrected:g} clamped "
                        f"to adapter rpm_max {limit:g}")
            corrected = limit
        self._event(f"mach loop: measured Mach {meas:.3f} off target "
                    f"{mach:g} — correcting to {corrected:g} RPM")
        return corrected
