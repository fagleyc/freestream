"""LswtDrive — host-side controller for one LSWT fan (ABB ACS530).

One control thread polls the drive's actual output frequency
(wire 102, Hz×10) at ``poll_s`` and RAMPS the commanded reference
toward the setpoint at ``ramp_hz_per_s`` — the commanded value never
step-jumps the fan. This host-side ramp deliberately REPLACES the
deployed C#'s crude protection (HwControllerVelocityLSWT_ACB530.cs
``setMotorCntrlrVelocity`` lines 158–172: a requested change of more
than 2 ft/s commanded reference **0**, slamming the fan toward zero).

Safety model
------------
* **ARM gating** lives in the GUI (no start/stop/setpoint until armed;
  mirrors tunnel_plc); the driver itself refuses commands while
  disconnected.
* ``fan_start()`` writes the ABB START word (1151) and anchors the
  ramp at the CURRENT actual speed so a start never step-jumps.
* ``fan_stop()`` writes the STOP word (1150) **and zeroes the
  reference**, synchronously from the calling thread.
* ``estop()`` = immediate STOP word + zero reference from the CALLING
  thread (not queued behind the control loop), like traverse
  ``stop_all``. Always safe to call.
* **Comm loss = alert, NOT auto-stop.** If no poll succeeds within
  ``stale_after_s`` the status goes STALE and the operator is alerted,
  but the fan is deliberately NOT stopped: the ACS530 holds its last
  commanded reference safely on its own, and auto-stopping would turn
  a transient network blip into an aborted run and an uncommanded
  flow change mid-test. The physical console / E-STOP remains the
  backstop, exactly as with the deployed C# tool.

Velocity is published through the measured 61-point calibration
(``lswt.calibration``); ``set_velocity(fps)`` maps through
``fps_to_hz``. Sim mode swaps in :class:`~lswt.emulator.SimAcs530`
(first-order fan, ~3 s time constant) so the GUI runs full-featured
without hardware.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

from . import calibration
from .config import LswtConfig
from .datamodel import ScanRingBuffer
from .drive import CMD_START, CMD_STOP, AbbAcs530, LswtError, \
    reference_counts
from .emulator import SimAcs530

log = logging.getLogger(__name__)

FIELDS = ["t", "actual_hz", "velocity_fps", "cmd_hz", "set_hz"]


class LswtDrive:
    """One LSWT fan drive (ramped reference, calibrated velocity)."""

    def __init__(self, config: Optional[LswtConfig] = None):
        self.config = config or LswtConfig()
        self.on_status: Optional[Callable[[str], None]] = None
        self.ring = ScanRingBuffer(FIELDS, capacity=200_000)

        self._drive = None             # AbbAcs530 | SimAcs530
        self._connected = False
        self._sim = False
        self._running = False          # host belief: START word sent
        self._setpoint_hz = 0.0
        self._cmd_hz = 0.0             # ramped commanded value
        self._cmd_counts = 0           # last reference counts written
        self._actual_hz = 0.0          # magnitude (abs of the register)
        self._last_ok = 0.0            # perf_counter of last good poll
        self._errors = 0
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()  # command/state lock

    # ── public state ─────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def sim_mode(self) -> bool:
        return self._sim

    def state(self) -> dict:
        with self._lock:
            age = (time.perf_counter() - self._last_ok
                   if self._last_ok else float("inf"))
            fps = calibration.hz_to_fps(self._actual_hz)
            return {
                "connected": self._connected,
                "sim": self._sim,
                "running": self._running,
                "actual_hz": self._actual_hz,
                "velocity_fps": fps,
                "setpoint_hz": self._setpoint_hz,
                "cmd_hz": self._cmd_hz,
                "ramping": (self._running and
                            abs(self._cmd_hz - self._setpoint_hz) > 1e-6),
                "stale": (self._connected and
                          age > self.config.stale_after_s),
                "age_s": age if self._connected else float("inf"),
            }

    def set_config(self, config: LswtConfig) -> None:
        """Adopt a new configuration (limits/ramp apply immediately;
        IP/port/sign at the next connect)."""
        with self._lock:
            self.config = config

    # ── lifecycle ────────────────────────────────────────────────────────
    def connect(self) -> None:
        if self._connected:
            return
        self._sim = self.config.force_sim
        if self._sim:
            self._drive = SimAcs530(self.config.reference_sign,
                                    self.config.sim_tau_s)
        else:
            self._drive = AbbAcs530(self.config.ip, self.config.port,
                                    self.config.unit_id,
                                    self.config.modbus_timeout_s,
                                    self.config.reference_sign)
        self._drive.connect()
        # connect is READ-PASSIVE: no control/reference writes, so a
        # host reconnect never disturbs a fan already running under a
        # previous session. The first poll anchors the actual speed.
        hz = abs(self._drive.read_actual_hz())
        with self._lock:
            self._actual_hz = hz
            self._running = False      # host belief resets — re-arm to act
            self._setpoint_hz = 0.0
            self._cmd_hz = 0.0
            self._cmd_counts = 0
            self._last_ok = time.perf_counter()
        self._errors = 0
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="lswt-loop", daemon=True)
        self._thread.start()
        self._connected = True
        mode = "SIM" if self._sim else "LIVE"
        self._status(f"Connected ({mode}) — {self.config.label} drive at "
                     f"{self.config.ip}, actual {hz:.1f} Hz")
        if hz > 0.5:
            self._status(f"NOTE: fan already turning at {hz:.1f} Hz — "
                         f"start was not commanded by this session")

    def disconnect(self) -> None:
        if not self._connected:
            return
        try:
            self.fan_stop()
        except Exception as exc:                       # noqa: BLE001
            log.warning("stop during disconnect: %s", exc)
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._drive is not None:
            self._drive.close()
            self._drive = None
        self._connected = False
        self._status("Disconnected")

    # ── commands ─────────────────────────────────────────────────────────
    def set_hz(self, hz: float) -> None:
        """Set the frequency setpoint (clamped 0..max_hz). The control
        loop ramps the commanded reference toward it."""
        hz = max(0.0, min(self.config.max_hz, float(hz)))
        with self._lock:
            self._setpoint_hz = hz
        self._status(f"Setpoint {hz:.1f} Hz "
                     f"({calibration.hz_to_fps(hz):.1f} ft/s)")

    def set_velocity(self, fps: float) -> None:
        """Set the setpoint as a tunnel velocity (ft/s) via the
        measured calibration."""
        self.set_hz(calibration.fps_to_hz(max(0.0, float(fps))))

    def fan_start(self) -> None:
        """Write the ABB START word (1151). The ramp anchors at the
        current actual speed so the start never step-jumps."""
        if not self._connected or self._drive is None:
            raise LswtError("connect() first")
        with self._lock:
            anchor = self._actual_hz
        self._drive.write_control(CMD_START)
        with self._lock:
            self._running = True
            self._cmd_hz = anchor
        self._status(f"FAN START ({self.config.label}) — ramping toward "
                     f"{self._setpoint_hz:.1f} Hz at "
                     f"{self.config.ramp_hz_per_s:g} Hz/s")

    def fan_stop(self) -> None:
        """STOP word (1150) + zero reference, from the calling thread."""
        if not self._connected or self._drive is None:
            return
        self._drive.write_control(CMD_STOP)
        self._drive.write_reference(0)
        with self._lock:
            self._running = False
            self._setpoint_hz = 0.0
            self._cmd_hz = 0.0
            self._cmd_counts = 0
        self._status(f"FAN STOP ({self.config.label}) — reference zeroed")

    def estop(self) -> None:
        """Immediate STOP + zero reference from the CALLING thread (not
        queued behind the loop). Safe to call in any state."""
        with self._lock:
            self._running = False
            self._setpoint_hz = 0.0
            self._cmd_hz = 0.0
            self._cmd_counts = 0
        if self._drive is not None and self._drive.connected:
            try:
                self._drive.write_control(CMD_STOP)
                self._drive.write_reference(0)
            except LswtError as exc:
                self._status(f"E-STOP write FAILED: {exc} — use the "
                             f"physical console stop")
                return
        self._status("E-STOP — fan stop commanded, reference zeroed")

    # ── control loop ─────────────────────────────────────────────────────
    def _loop(self) -> None:
        t_prev = time.perf_counter()
        while not self._stop_evt.is_set():
            try:
                t_prev = self._tick(t_prev)
            except Exception as exc:                   # noqa: BLE001
                # nothing may kill this thread; comm loss is ALERT-ONLY
                log.exception("control loop error")
                self._status(f"CONTROL LOOP ERROR: {exc}")
            self._stop_evt.wait(self.config.poll_s)

    def _tick(self, t_prev: float) -> float:
        now = time.perf_counter()
        dt = min(now - t_prev, 2.0)

        # ── poll actual speed ──
        try:
            hz = abs(self._drive.read_actual_hz())
            with self._lock:
                self._actual_hz = hz
                self._last_ok = now
            if self._errors:
                self._status("drive comms recovered")
            self._errors = 0
        except LswtError as exc:
            self._errors += 1
            if self._errors in (1, 5) or self._errors % 20 == 0:
                # alert only — deliberately NO auto-stop on comm loss
                # (the drive holds its reference safely; see module doc)
                self._status(f"drive poll error ({self._errors}): {exc}")

        # ── ramp the commanded reference toward the setpoint ──
        with self._lock:
            running = self._running
            target = self._setpoint_hz
            cmd = self._cmd_hz
        if running:
            step = self.config.ramp_hz_per_s * dt
            if cmd < target:
                cmd = min(cmd + step, target)
            elif cmd > target:
                cmd = max(cmd - step, target)
            cmd = max(0.0, min(self.config.max_hz, cmd))
            counts = reference_counts(cmd)
            write_needed = False
            with self._lock:
                if self._running:          # not stopped meanwhile
                    self._cmd_hz = cmd
                    if counts != self._cmd_counts:
                        self._cmd_counts = counts
                        write_needed = True
            if write_needed:
                try:
                    self._drive.write_reference(counts)
                except LswtError as exc:
                    self._errors += 1
                    self._status(f"reference write error: {exc}")

        # ── publish ──
        with self._lock:
            sample = {
                "t": np.array([time.time()]),
                "actual_hz": np.array([self._actual_hz]),
                "velocity_fps": np.array(
                    [calibration.hz_to_fps(self._actual_hz)]),
                "cmd_hz": np.array([self._cmd_hz]),
                "set_hz": np.array([self._setpoint_hz]),
            }
        self.ring.push_block(sample)
        return now

    # ── helpers ──────────────────────────────────────────────────────────
    def _status(self, msg: str) -> None:
        log.info(msg)
        if self.on_status:
            self.on_status(msg)
