"""LswtTunnelAdapter — HAL SetpointDevice over lswt.LswtDrive (ABB ACS530).

The North/South LSWT fans run on an ABB ACS530 VFD; the drive exposes
OUTPUT FREQUENCY (Hz), not shaft RPM, and tunnel velocity comes from the
measured 61-point hz↔fps calibration (``lswt.calibration``). The natural
setpoint is therefore Hz (or velocity in ft/s); the HAL/sweep engine
speaks ``rpm`` (``set_target(rpm=...)``, ``readback()["rpm"]``,
/Tunnel RPM_cmd/RPM_meas — see :class:`freestream.hal.SetpointDevice`).

RPM equivalence — stated honestly: this adapter maps ``rpm`` ⇄ Hz with a
fixed factor of 60 (cycles/s → cycles/min, i.e. the 2-pole synchronous
speed equivalent). The fan's true shaft RPM depends on motor pole count
and slip, which the drive does not report; the recorded RPM_cmd/RPM_meas
are exactly ``Hz × 60`` and the readback also carries the raw ``hz`` /
``hz_set`` and the calibrated ``velocity_fps`` so nothing is lost.

Driver constraints honoured (see lswt.device):

* The control loop only ramps the reference while the fan is RUNNING
  (START word sent). In SIM :meth:`set_target` pulses ``fan_start()``
  when needed so sweeps work out of the box (mirrors TunnelAdapter);
  on hardware starting the fan stays a deliberate operator action.
* Comm loss is alert-only in the driver (STALE, never auto-stop);
  here a stale snapshot reports OFFLINE and ``at_target()`` is False.
* ``estop()`` passthrough: immediate STOP word + zero reference from
  the calling thread — always safe.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

_DEVICES_DIR = Path(__file__).resolve().parents[2] / "devices"
if str(_DEVICES_DIR) not in sys.path:
    sys.path.insert(0, str(_DEVICES_DIR))

from lswt import calibration                                  # noqa: E402
from lswt.config import LswtConfig                            # noqa: E402
from lswt.device import LswtDrive                             # noqa: E402

from ..hal import DeviceStatus, OFFLINE, OK                   # noqa: E402
from ._configurable import ConfigurableAdapter                 # noqa: E402

#: rpm ⇄ hz equivalence: cycles/min per cycle/s (2-pole synchronous
#: speed — NOT shaft RPM; see module docstring)
RPM_PER_HZ = 60.0


@dataclass
class LswtSnapshot:
    """Dashboard-shaped snapshot (duck-typed like TunnelSnapshot: the
    tunnel dashboard reads ``actual_rpm``/``rpm_set``/``fan_running``/
    ``stale``; missing lamp attrs default False via getattr)."""
    actual_hz: float = 0.0
    setpoint_hz: float = 0.0
    cmd_hz: float = 0.0
    velocity_fps: float = 0.0
    actual_rpm: float = 0.0          # Hz × 60 (see module docstring)
    rpm_set: float = 0.0             # setpoint Hz × 60
    fan_running: bool = False
    ramping: bool = False
    stale: bool = False
    age_s: float = float("inf")


class LswtTunnelAdapter(ConfigurableAdapter):
    """SetpointDevice adapter for the North LSWT fan (ABB ACS530)."""

    id = "lswt"
    label = "North LSWT fan (ABB ACS530)"
    settings_dialog_path = "lswt.app.settings_dialog:SettingsDialog"

    def __init__(self, sim: bool = False,
                 config_path: Optional[str] = None,
                 tunnel: str = "north",
                 hz_tol: float = 0.5):
        cfg = (LswtConfig.load(config_path) if config_path
               else LswtConfig.for_tunnel(tunnel))
        cfg.force_sim = bool(sim)
        self._cfg = cfg
        self._dev = LswtDrive(cfg)
        self._sim = bool(sim)
        self._hz_tol = float(hz_tol)
        self.label = f"{cfg.label} fan (ABB ACS530)"

    # ── DeviceBase ───────────────────────────────────────────────────────
    def connect(self) -> None:
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
        st = self._dev.state()
        age = None if st["age_s"] == float("inf") else st["age_s"]
        if st["stale"]:
            return DeviceStatus(state=OFFLINE, sim=self._sim,
                                message=f"drive poll stale "
                                        f"({st['age_s']:.1f}s) — fan "
                                        f"holds its last reference",
                                last_sample_age_s=age)
        return DeviceStatus(state=OK, sim=self._sim,
                            last_sample_age_s=age)

    # ── SetpointDevice ───────────────────────────────────────────────────
    def set_target(self, **kw: float) -> None:
        """Set the fan setpoint. Exactly one of:

        * ``hz=``       — drive output frequency (native),
        * ``velocity=`` — tunnel velocity in ft/s (measured calibration),
        * ``rpm=``      — the HAL/sweep convention, mapped as Hz × 60.

        The driver ramps the commanded reference (``ramp_hz_per_s``) and
        clamps to ``config.max_hz``. In SIM the fan is auto-started when
        needed (the sim plant only spools while running); on hardware
        starting the fan is a deliberate operator action.
        """
        keys = set(kw) & {"hz", "velocity", "rpm"}
        if len(keys) != 1 or set(kw) - {"hz", "velocity", "rpm"}:
            raise ValueError(
                f"lswt setpoint is ONE of hz=/velocity=(fps)/rpm= "
                f"(rpm = Hz*60 equivalence); got {sorted(kw)}")
        if "hz" in kw:
            hz = float(kw["hz"])
        elif "velocity" in kw:
            hz = calibration.fps_to_hz(max(0.0, float(kw["velocity"])))
        else:
            hz = float(kw["rpm"]) / RPM_PER_HZ
        self._dev.set_hz(hz)
        if self._sim and hz > 0 and not self._dev.state()["running"]:
            self._dev.fan_start()          # sim only spools while running

    def at_target(self) -> bool:
        st = self._dev.state()
        if st["stale"]:
            return False
        if st["setpoint_hz"] > 0 and not st["running"]:
            return False                   # fan not started yet
        return abs(st["actual_hz"] - st["setpoint_hz"]) <= self._hz_tol

    def readback(self) -> Dict[str, float]:
        """Actual/setpoint state: native Hz + calibrated velocity, plus
        the HAL ``rpm``/``rpm_set`` keys (Hz × 60 equivalence) that the
        sweep engine records as RPM_meas/RPM_cmd and the dashboard polls.
        """
        st = self._dev.state()
        return {
            "hz": st["actual_hz"],
            "hz_set": st["setpoint_hz"],
            "cmd_hz": st["cmd_hz"],
            "velocity_fps": st["velocity_fps"],
            "rpm": st["actual_hz"] * RPM_PER_HZ,
            "rpm_set": st["setpoint_hz"] * RPM_PER_HZ,
        }

    # ── fan lifecycle / safety passthroughs ──────────────────────────────
    def fan_start(self) -> None:
        """START word — ramp anchors at the current actual speed."""
        self._dev.fan_start()

    def fan_stop(self) -> None:
        """STOP word + zero reference (synchronous)."""
        self._dev.fan_stop()

    def estop(self) -> None:
        """Immediate STOP + zero reference from the calling thread."""
        self._dev.estop()

    # ── tunnel-environment widgets (Freestream dashboard) ────────────────
    def snapshot(self) -> LswtSnapshot:
        """Duck-typed snapshot for the tunnel dashboard (actual_rpm /
        rpm_set / fan_running / stale; RPM fields are Hz × 60)."""
        st = self._dev.state()
        return LswtSnapshot(
            actual_hz=st["actual_hz"],
            setpoint_hz=st["setpoint_hz"],
            cmd_hz=st["cmd_hz"],
            velocity_fps=st["velocity_fps"],
            actual_rpm=st["actual_hz"] * RPM_PER_HZ,
            rpm_set=st["setpoint_hz"] * RPM_PER_HZ,
            fan_running=bool(st["running"]),
            ramping=bool(st["ramping"]),
            stale=bool(st["stale"]),
            age_s=st["age_s"],
        )
