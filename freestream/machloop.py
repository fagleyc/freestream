"""MachLoop — tunnel targeting strategy: target Mach → fan-RPM command.

The tunnel PLC (Red Lion → GE PLCs) exposes fan RPM, not Mach, and the
tunnel_plc driver stays raw/standalone by project rule — so Mach lives
HERE, in the Freestream composition layer, never in the driver:

* target Mach → RPM via ``config.rpm_per_mach`` (a rig-tuned linear map),
  always clamped to ``[0, rpm_max]`` from the tunnel adapter's own config;
* the RPM command goes through the ordinary HAL
  :class:`~freestream.hal.SetpointDevice` (``set_target(rpm=...)``);
* *at target* is decided from the MEASURED Mach — the isentropic chain in
  :mod:`freestream.derived` over the DaqBook-group streaming device's
  ``latest()`` (Pdiff/Ptot/Temp engineering units) — within
  ``config.mach_tolerance``, AND the RPM readback settled;
* in SIM the sim DaqBook pressures do not respond to fan RPM, so the loop
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

from .config import FreestreamConfig
from .derived import tunnel_state
from .hal import SetpointDevice, Streaming

log = logging.getLogger(__name__)

#: group name of the tunnel-conditions DAQ (see adapters.daqbook.GROUP)
DAQ_GROUP = "DaqBook2005"

#: WaitFn(condition, timeout_s, fail_msg) — the sweep engine's abortable
#: _wait; tests may pass a simple polling loop.
WaitFn = Callable[[Callable[[], bool], float, str], None]


def find_tunnel_daq(streams) -> Optional[Streaming]:
    """The DaqBook-group streaming device (Pdiff/Ptot/Temp), if present."""
    for s in streams:
        try:
            if any(ch.group == DAQ_GROUP for ch in s.channels()):
                return s
        except Exception:                              # noqa: BLE001
            continue
    return None


def daq_mach(daq: Optional[Streaming]) -> Optional[float]:
    """Isentropic Mach from a DaqBook stream's ``latest()`` — the ONE
    measurement shared by MachLoop closure and the sweep engine's
    monitor-only operator wait. None when unavailable/invalid."""
    if daq is None:
        return None
    try:
        vals = daq.latest()
    except Exception:                                  # noqa: BLE001
        return None
    if not all(k in vals for k in ("Pdiff", "Ptot", "Temp")):
        return None
    st = tunnel_state(float(vals["Pdiff"]), float(vals["Ptot"]),
                      float(vals["Temp"]))
    return st.mach if st.valid else None


def make_tunnel_measure(daq: Optional[Streaming],
                        setpoint: Optional[SetpointDevice]
                        ) -> Callable[[], Tuple[float, float]]:
    """Build the ``() -> (measured_mach, rpm_meas)`` probe used by the
    monitor-only operator wait (sweep.OperatorWaitRequest.measure):
    isentropic Mach from the DAQ (NaN when unavailable) + fan RPM from
    the setpoint readback — reads only, never a command."""
    def measure() -> Tuple[float, float]:
        mach = daq_mach(daq)
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
                 daq: Optional[Streaming] = None,
                 event: Optional[Callable[[str], None]] = None):
        self.setpoint = setpoint
        self.config = config
        self.daq = daq
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
        """Isentropic Mach from the DAQ's latest() (None if unavailable)."""
        return daq_mach(self.daq)

    @property
    def live(self) -> bool:
        """True when measured-Mach closure is possible (hardware DAQ)."""
        if self.daq is None or getattr(self.daq, "sim", True):
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
            # sim plant: DaqBook pressures don't respond to fan RPM
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
                    "DaqBook stream — cannot confirm tunnel condition")
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
