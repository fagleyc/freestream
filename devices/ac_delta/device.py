"""CrescentDrive — dual-axis host-side position controller.

Runs the position loop for both axes in ONE control thread, so Alpha and
Beta move **synchronously**: a `move_to(alpha=…, beta=…)` command starts
both in the same tick and each axis picks its own speed step from its own
remaining distance every tick (deceleration bands from config).

Safety model
------------
* Soft travel limits enforced before any move is accepted.
* `stop_all()` / E-stop writes the stop command to both drives immediately
  from the calling thread (not queued behind the loop).
* Modbus watchdog: `max_consecutive_errors` failures → stop everything,
  drop the connection, report via `on_status`.
* Angles are only trusted when the axis is marked `calibrated`; the GUI
  shows raw encoder counts regardless.

Lifecycle/callback shape matches the other AeroVIS device drivers
(`connect/start/stop/disconnect`, `on_status`, device-owned ring buffer of
the angle history).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np

from .axis import AxisError, CrescentAxis
from .config import AxisConfig, CrescentConfig
from .datamodel import ScanRingBuffer
from .emulator import SimAxis

log = logging.getLogger(__name__)

FIELDS = ["t", "Alpha", "Beta", "Alpha_enc", "Beta_enc",
          "Alpha_moving", "Beta_moving"]


class _AxisState:
    def __init__(self, cfg: AxisConfig):
        self.cfg = cfg
        self.axis = None                    # CrescentAxis | SimAxis
        self.target: Optional[float] = None
        self.moving = False
        self.jogging = False
        self.jog_forward = True
        self.angle = 0.0
        self.encoder = 0
        self.errors = 0


class CrescentDrive:
    """Dual-axis crescent drive with a synchronous host position loop."""

    def __init__(self, config: Optional[CrescentConfig] = None):
        self.config = config or CrescentConfig()

        self.on_status: Optional[Callable[[str], None]] = None
        self.on_move_complete: Optional[Callable[[str], None]] = None

        self.ring = ScanRingBuffer(FIELDS, capacity=200_000)

        self._alpha = _AxisState(self.config.alpha)
        self._beta = _AxisState(self.config.beta)

        self._connected = False
        self._sim = False
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cmd_lock = threading.Lock()

    # ── public state ─────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def sim_mode(self) -> bool:
        return self._sim

    def state(self) -> Dict[str, dict]:
        out = {}
        for s in (self._alpha, self._beta):
            out[s.cfg.name] = {
                "angle": s.angle, "encoder": s.encoder,
                "moving": s.moving, "jogging": s.jogging,
                "target": s.target,
                "calibrated": s.cfg.calibrated,
            }
        return out

    # ── configuration ────────────────────────────────────────────────────
    def set_config(self, config: CrescentConfig) -> None:
        """Adopt a new configuration (e.g. after loading a JSON file).

        Rebinds the axis states — and, when connected, the live axis
        protocol objects — to the new AxisConfig instances, so calibration
        /limits/tolerances take effect immediately. Without this, a loaded
        config leaves the drive reading the ORIGINAL config objects and
        e.g. calibration is silently ignored (the Motion tab stays in
        encoder counts). IPs/ports still only apply at the next connect.
        """
        with self._cmd_lock:
            self.config = config
            for st, ax_cfg in ((self._alpha, config.alpha),
                               (self._beta, config.beta)):
                st.cfg = ax_cfg
                if st.axis is not None:
                    st.axis.cfg = ax_cfg
                # re-derive the displayed angle under the new calibration
                st.angle = ax_cfg.encoder_to_angle(st.encoder)

    # ── lifecycle ────────────────────────────────────────────────────────
    def connect(self) -> None:
        if self._connected:
            return
        self._sim = self.config.force_sim
        for st in (self._alpha, self._beta):
            if self._sim:
                st.axis = SimAxis(st.cfg)
            else:
                st.axis = CrescentAxis(st.cfg, self.config.modbus_timeout_s)
            st.axis.connect()
            st.target = None
            st.moving = False
            st.errors = 0
        # initial position read
        for st in (self._alpha, self._beta):
            st.encoder = st.axis.read_encoder()
            st.angle = st.cfg.encoder_to_angle(st.encoder)

        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="crescent-loop", daemon=True)
        self._thread.start()
        self._connected = True
        mode = "SIM" if self._sim else "LIVE"
        self._status(f"Connected ({mode}) — Alpha {self._alpha.angle:+.2f}°, "
                     f"Beta {self._beta.angle:+.2f}°")
        if not (self.config.alpha.calibrated and
                self.config.beta.calibrated) and not self._sim:
            self._status("WARNING: axis calibration not entered — angles "
                         "are not trustworthy until calibrated")

    def disconnect(self) -> None:
        if not self._connected:
            return
        try:
            self.stop_all()
        except Exception as exc:                       # noqa: BLE001
            log.warning("stop during disconnect: %s", exc)
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        for st in (self._alpha, self._beta):
            if st.axis is not None:
                st.axis.close()
                st.axis = None
        self._connected = False
        self._status("Disconnected")

    # ── motion commands ──────────────────────────────────────────────────
    def move_to(self, alpha: Optional[float] = None,
                beta: Optional[float] = None) -> None:
        """Start a move; both axes begin in the same control tick.

        Pass one or both targets. Raises on limit violations before any
        motion starts.
        """
        if not self._connected:
            raise RuntimeError("connect() first")
        for value, st in ((alpha, self._alpha), (beta, self._beta)):
            if value is None:
                continue
            if not st.cfg.calibrated:
                raise ValueError(f"{st.cfg.name} is not calibrated — "
                                 f"angle moves disabled (jog only)")
            lo, hi = st.cfg.min_deg, st.cfg.max_deg
            if not lo <= value <= hi:
                raise ValueError(f"{st.cfg.name} target {value:+.2f}° "
                                 f"outside limits [{lo:+.1f}, {hi:+.1f}]")
        with self._cmd_lock:
            for value, st in ((alpha, self._alpha), (beta, self._beta)):
                if value is None:
                    continue
                st.target = float(value)
                st.moving = abs(st.angle - st.target) > st.cfg.tolerance_deg
        started = [st.cfg.name for st in (self._alpha, self._beta)
                   if st.moving]
        if started:
            self._status("Moving " + " + ".join(
                f"{st.cfg.name}→{st.target:+.2f}°"
                for st in (self._alpha, self._beta) if st.moving))

    def jog(self, name: str, forward: bool, step: int = 2) -> None:
        """Hold-to-run jog at a fixed speed step (no target, no cal needed).

        Call :meth:`jog_stop` (or release the GUI button) to stop. While
        calibrated, the control loop enforces the soft limits during a jog;
        uncalibrated jogging has NO limit protection — watch the hardware.
        """
        if not self._connected:
            raise RuntimeError("connect() first")
        st = self._alpha if name.lower() == "alpha" else self._beta
        with self._cmd_lock:
            st.moving = False
            st.target = None
            st.jogging = True
            st.jog_forward = forward
            st.axis.command_step(max(1, min(step, 5)), forward=forward)
        self._status(f"{st.cfg.name} jog "
                     f"{'+' if forward else '−'} (step {step})")

    def jog_stop(self, name: str) -> None:
        st = self._alpha if name.lower() == "alpha" else self._beta
        with self._cmd_lock:
            st.jogging = False
            if st.axis is not None:
                try:
                    st.axis.stop()
                except AxisError as exc:
                    self._status(f"{st.cfg.name} jog stop failed: {exc}")
        self._status(f"{st.cfg.name} jog stop")

    def stop_all(self) -> None:
        """Immediate stop of both axes (E-stop path, synchronous)."""
        with self._cmd_lock:
            for st in (self._alpha, self._beta):
                st.moving = False
                st.jogging = False
                st.target = None
                if st.axis is not None and st.axis.connected:
                    try:
                        st.axis.stop()
                    except AxisError as exc:
                        self._status(f"STOP write failed on "
                                     f"{st.cfg.name}: {exc}")
        self._status("STOP issued to both axes")

    def stop_axis(self, name: str) -> None:
        st = self._alpha if name.lower() == "alpha" else self._beta
        with self._cmd_lock:
            st.moving = False
            st.jogging = False
            st.target = None
            if st.axis is not None:
                st.axis.stop()
        self._status(f"{st.cfg.name} stopped")

    # ── control loop ─────────────────────────────────────────────────────
    def _loop(self) -> None:
        period = self.config.loop_ms / 1000.0
        bands = self.config.speed_bands_deg
        max_step = max(1, min(self.config.max_step, 5))
        while not self._stop_evt.is_set():
            t0 = time.perf_counter()
            any_completed = []
            with self._cmd_lock:
                for st in (self._alpha, self._beta):
                    if st.axis is None:
                        continue
                    try:
                        st.encoder = st.axis.read_encoder()
                        st.angle = st.cfg.encoder_to_angle(st.encoder)
                        st.errors = 0
                    except AxisError as exc:
                        st.errors += 1
                        self._status(f"{st.cfg.name} read error "
                                     f"({st.errors}): {exc}")
                        if st.errors >= self.config.max_consecutive_errors:
                            self._watchdog_trip(st)
                        continue

                    # jog: no target loop, but enforce soft limits when the
                    # angle can be trusted (calibrated axis)
                    if st.jogging:
                        if st.cfg.calibrated and (
                                (st.jog_forward and
                                 st.angle >= st.cfg.max_deg) or
                                (not st.jog_forward and
                                 st.angle <= st.cfg.min_deg)):
                            st.jogging = False
                            try:
                                st.axis.stop()
                            except AxisError:
                                pass
                            self._status(f"{st.cfg.name} jog stopped at "
                                         f"soft limit "
                                         f"({st.angle:+.2f}°)")
                        continue

                    if not st.moving or st.target is None:
                        continue
                    delta = st.target - st.angle
                    if abs(delta) < st.cfg.tolerance_deg:
                        st.moving = False
                        try:
                            st.axis.stop()
                        except AxisError as exc:
                            self._status(f"{st.cfg.name} stop failed: {exc}")
                        any_completed.append(st.cfg.name)
                        continue
                    step = self._pick_step(abs(delta), bands, max_step)
                    try:
                        st.axis.command_step(step, forward=(delta > 0))
                    except AxisError as exc:
                        st.errors += 1
                        self._status(f"{st.cfg.name} command error: {exc}")
                        if st.errors >= self.config.max_consecutive_errors:
                            self._watchdog_trip(st)

            self.ring.push_block({
                "t": np.array([time.time()]),
                "Alpha": np.array([self._alpha.angle]),
                "Beta": np.array([self._beta.angle]),
                "Alpha_enc": np.array([float(self._alpha.encoder)]),
                "Beta_enc": np.array([float(self._beta.encoder)]),
                "Alpha_moving": np.array([float(self._alpha.moving)]),
                "Beta_moving": np.array([float(self._beta.moving)]),
            })
            for name in any_completed:
                self._status(f"{name} move complete")
                if self.on_move_complete:
                    self.on_move_complete(name)

            elapsed = time.perf_counter() - t0
            self._stop_evt.wait(max(period - elapsed, 0.005))

    @staticmethod
    def _pick_step(delta_abs: float, bands: List[float],
                   max_step: int) -> int:
        for i, threshold in enumerate(bands):
            if delta_abs < threshold:
                return min(i + 1, max_step)
        return min(len(bands) + 1, max_step)

    def _watchdog_trip(self, st: _AxisState) -> None:
        self._status(f"WATCHDOG: {st.cfg.name} unreachable — stopping all "
                     f"axes and disconnecting it")
        for other in (self._alpha, self._beta):
            other.moving = False
            other.target = None
            if other.axis is not None and other.axis.connected:
                try:
                    other.axis.stop()
                except AxisError:
                    pass
        st.axis.close()

    # ── helpers ──────────────────────────────────────────────────────────
    def _status(self, msg: str) -> None:
        log.info(msg)
        if self.on_status:
            self.on_status(msg)
