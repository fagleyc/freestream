"""StingDrive — dual-axis LSWT sting controller (alpha/beta indexers).

Same lifecycle/callback shape as the other AeroVIS device drivers
(``connect``/``disconnect``, ``on_status``/``on_move_complete``,
device-owned ring buffer of the angle history), and the same safety shell
as the crescent drive — but the position loop runs INSIDE the serial
indexers: the host loads a relative distance (``D``) and starts the move
(``G``), then polls status (``R``) until READY.

Safety model
------------
* Soft travel limits enforced before any command is sent; absolute moves
  additionally require the axis to be zeroed (open-loop step counter).
* ``stop_all()`` / E-stop writes ``1S``/``2S`` immediately from the calling
  thread — no echo wait, no queueing behind the poll loop.
* ``*S`` (stall) or a move timeout latches a FAULT: everything is stopped,
  further motion is refused until ``reset_fault()``; a stall additionally
  requires the drives to be power-cycled (legacy behaviour) and the axes
  re-zeroed. ``reinitialize(confirm_safe=True)`` re-runs the init sequence
  (includes the ``Z`` drive reset, which the legacy tool warns may cause
  uncontrolled movement if the sting is not in a safe position).
* Serial watchdog: ``max_consecutive_errors`` failures in the poll loop →
  stop everything, drop the connection, report via ``on_status``.

The deployed C# tool parked Alpha at ~+29.3° on shutdown
(``park_on_disconnect``, default OFF here — enable once limits are
confirmed on the real rig).
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from datetime import datetime
from typing import Callable, Dict, Optional

import numpy as np

from .config import StingAxisConfig, StingConfig
from .datamodel import ScanRingBuffer
from .emulator import SimSerial
from .protocol import BUSY, READY, STALLED, StingError, StingProtocol

log = logging.getLogger(__name__)

FIELDS = ["t", "Alpha", "Beta", "Alpha_steps", "Beta_steps",
          "Alpha_moving", "Beta_moving"]


class _AxisState:
    def __init__(self, cfg: StingAxisConfig):
        self.cfg = cfg
        self.counts = 0
        self.angle = 0.0
        self.moving = False
        self.target: Optional[float] = None
        self.deadline = 0.0
        self.responding = False

    def update_from_counts(self, counts: int) -> None:
        self.counts = counts
        self.angle = self.cfg.counts_to_angle(counts)


class StingDrive:
    """Dual-axis LSWT sting drive over one RS-232 daisy chain."""

    def __init__(self, config: Optional[StingConfig] = None):
        self.config = config or StingConfig()

        self.on_status: Optional[Callable[[str], None]] = None
        self.on_move_complete: Optional[Callable[[str], None]] = None

        self.ring = ScanRingBuffer(FIELDS, capacity=200_000)
        self._last_state_save = 0.0

        self._alpha = _AxisState(self.config.alpha)
        self._beta = _AxisState(self.config.beta)

        self._proto: Optional[StingProtocol] = None
        self._connected = False
        self._sim = False
        self._fault: Optional[str] = None
        self._errors = 0
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

    @property
    def fault(self) -> Optional[str]:
        return self._fault

    @property
    def moving(self) -> bool:
        return self._alpha.moving or self._beta.moving

    def state(self) -> Dict[str, dict]:
        out: Dict[str, dict] = {}
        for st in (self._alpha, self._beta):
            out[st.cfg.name] = {
                "angle": st.angle, "counts": st.counts,
                "moving": st.moving, "target": st.target,
                "zeroed": st.cfg.zeroed, "enabled": st.cfg.enabled,
                "responding": st.responding,
            }
        out["fault"] = self._fault
        return out

    def _axes(self):
        return (self._alpha, self._beta)

    def _axis(self, name: str) -> _AxisState:
        return self._alpha if name.lower() == "alpha" else self._beta

    # ── configuration ────────────────────────────────────────────────────
    def set_config(self, config: StingConfig) -> None:
        """Adopt a new configuration (limits/zero take effect at once;
        COM port and motion parameters apply at the next connect)."""
        with self._cmd_lock:
            self.config = config
            for st, ax_cfg in ((self._alpha, config.alpha),
                               (self._beta, config.beta)):
                st.cfg = ax_cfg
                st.angle = ax_cfg.counts_to_angle(st.counts)

    # ── lifecycle ────────────────────────────────────────────────────────
    def connect(self) -> None:
        if self._connected:
            return
        cfg = self.config
        self._fault = None
        self._errors = 0
        self._sim = cfg.force_sim
        if self._sim:
            self._proto = StingProtocol(SimSerial(cfg))
        else:
            self._proto = StingProtocol.open(cfg.com_port, cfg.baud,
                                             cfg.serial_timeout_s)
        try:
            self._init_drives()
        except Exception:
            self._proto.close()
            self._proto = None
            raise
        self._restore_position()
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._poll_loop,
                                        name="sting-poll", daemon=True)
        self._thread.start()
        self._connected = True
        # no brake: if the process dies without disconnect(), park and
        # checkpoint on interpreter exit as a last line of defence
        atexit.register(self._atexit_disconnect)
        mode = "SIM" if self._sim else f"LIVE on {cfg.com_port}"
        self._status(
            f"Connected ({mode}) — Alpha {self._alpha.angle:+.2f}°, "
            f"Beta {self._beta.angle:+.2f}°")
        if not all(a.cfg.zeroed for a in self._axes() if a.cfg.enabled):
            self._status("WARNING: axis not zeroed — set the sting to a "
                         "known angle and Zero before absolute moves")

    def _init_drives(self) -> None:
        """Bring-up sequence, mirroring the legacy ``InitHw`` order."""
        assert self._proto is not None
        p = self._proto
        cfg = self.config
        p.clear_input()
        # probe the chain (legacy sends 1R and checks for any response)
        try:
            p.status("1")
        except StingError as exc:
            raise StingError(
                f"Sting controllers are not responding on "
                f"{cfg.com_port} — is power on? ({exc})") from exc
        # Interface setup + reset are sent BLIND (same bytes as the
        # legacy tool, but no echo validation): SSI/SSA/Z change the
        # drives' echo behaviour while executing, so a strict echo read
        # here can time out even though the command was accepted.
        p.command_blind("", "SSI1")
        for st in self._axes():
            if not st.cfg.enabled:
                continue
            p.command_blind(st.cfg.unit, "SSA0")
            if cfg.init_reset:
                p.command_blind(st.cfg.unit, "Z")
        if cfg.init_reset:
            time.sleep(1.1)             # drives re-boot after Z (legacy)
            p.clear_input()
        # wait for READY; a stall at init is fatal (legacy behaviour).
        # Transient no-response is retried until the deadline — the
        # drives can stay quiet for a moment after the setup/reset.
        for st in self._axes():
            if not st.cfg.enabled:
                continue
            deadline = time.monotonic() + 5.0
            while True:
                try:
                    s = p.status(st.cfg.unit)
                except StingError:
                    s = None
                    p.clear_input()
                if s == READY:
                    st.responding = True
                    break
                if s == STALLED:
                    raise StingError(
                        f"{st.cfg.name} drive reports STALL at init — "
                        f"power-cycle the sting drives")
                if time.monotonic() > deadline:
                    raise StingError(
                        f"{st.cfg.name} drive not READY after reset — "
                        f"is power on?")
                time.sleep(0.1 if s is not None else 0.25)
        for st in self._axes():
            if st.cfg.enabled:
                p.command(st.cfg.unit, "LD3")
        for st in self._axes():
            if not st.cfg.enabled:
                continue
            st.update_from_counts(p.position(st.cfg.unit))
            p.command(st.cfg.unit, f"A{st.cfg.acceleration}")
            p.command(st.cfg.unit, f"AD{st.cfg.deceleration}")
            p.command(st.cfg.unit, f"V{st.cfg.velocity}")
            if st.cfg.brake_output:
                # OUT<n>B = output n follows Moving/Not-Moving: the
                # DRIVE releases the brake while stepping and engages
                # it when stopped/faulted/off. RAM setting on the SX —
                # (re)sent at every connect on purpose.
                p.command(st.cfg.unit,
                          f"OUT{st.cfg.brake_output}B")
                self._status(
                    f"{st.cfg.name} brake: O{st.cfg.brake_output} set "
                    f"to Moving/Not-Moving (released while moving, "
                    f"engaged at rest)")
        p.command_blind("", "FSD1")     # broadcast — echo not guaranteed

    def disconnect(self) -> None:
        if not self._connected:
            return
        try:
            self.stop_all()
        except Exception as exc:                    # noqa: BLE001
            log.warning("stop during disconnect: %s", exc)
        if (self.config.park_on_disconnect and self._fault is None
                and self._alpha.cfg.enabled and self._alpha.cfg.zeroed):
            try:
                self._park_alpha()
            except Exception as exc:                # noqa: BLE001
                self._status(f"Park failed: {exc}")
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._proto is not None:
            self._proto.close()
            self._proto = None
        self._connected = False
        self._save_state(clean=True)
        atexit.unregister(self._atexit_disconnect)
        self._status("Disconnected")

    def _atexit_disconnect(self) -> None:
        """Interpreter exiting with the drive still connected: park (if
        configured and safe) and checkpoint the position."""
        try:
            if self._connected:
                log.warning("process exiting while connected — parking "
                            "and saving sting position")
                self.disconnect()
        except Exception:                           # noqa: BLE001
            # last resort: at least persist what we know
            try:
                self._save_state(clean=False)
            except Exception:                       # noqa: BLE001
                pass

    # ── position persistence (no brake — safety) ─────────────────────────
    def _save_state(self, clean: bool) -> None:
        """Checkpoint the live position atomically; never raises."""
        try:
            path = self.config.resolved_state_path()
            state = {
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "clean": bool(clean),
                "axes": {
                    st.cfg.name: {
                        "angle": st.angle,
                        "counts": st.counts,
                        "zeroed": bool(st.cfg.zeroed),
                        "zero_offset_deg": st.cfg.zero_offset_deg,
                    } for st in self._axes()
                },
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except Exception as exc:                    # noqa: BLE001
            log.debug("state save failed: %s", exc)

    def _restore_position(self) -> None:
        """Re-establish the zero reference from the last checkpoint so
        an abrupt shutdown does not lose the open-loop position."""
        if not self.config.restore_position:
            return
        path = self.config.resolved_state_path()
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:                    # noqa: BLE001
            self._status(f"Could not read saved position: {exc}")
            return
        clean = bool(state.get("clean"))
        restored = []
        for st in self._axes():
            saved = state.get("axes", {}).get(st.cfg.name)
            if not (st.cfg.enabled and saved and saved.get("zeroed")):
                continue
            # map the CURRENT counter (0 after the Z reset, or whatever
            # the drive retained) onto the saved angle
            angle = float(saved["angle"])
            st.cfg.zero_offset_deg = (
                angle - st.cfg.direction * st.counts
                / st.cfg.steps_per_degree)
            st.cfg.zeroed = True
            st.update_from_counts(st.counts)
            restored.append(f"{st.cfg.name} {angle:+.2f}°")
        if not restored:
            return
        when = state.get("saved_at", "?")
        self._status(f"Restored last position ({', '.join(restored)}, "
                     f"saved {when})")
        if not clean:
            self._status(
                "WARNING: previous session did NOT shut down cleanly — "
                "the sting has no brake and may have moved. VERIFY the "
                "angle physically before absolute moves, or re-zero.")

    def _park_alpha(self) -> None:
        """Blocking park move (legacy 'off position', ~+29.3°)."""
        target = min(self.config.park_alpha_deg, self._alpha.cfg.max_deg)
        self._status(f"Parking Alpha at {target:+.1f}°…")
        self.move_to(alpha=target)
        deadline = time.monotonic() + 120.0
        while self._alpha.moving and time.monotonic() < deadline:
            time.sleep(0.2)

    # ── motion commands ──────────────────────────────────────────────────
    def _require_ready(self) -> None:
        if not self._connected:
            raise RuntimeError("connect() first")
        if self._fault:
            raise RuntimeError(f"FAULT latched: {self._fault} — "
                               f"reset_fault() required")

    def move_to(self, alpha: Optional[float] = None,
                beta: Optional[float] = None) -> None:
        """Absolute move of one or both axes (validates before any motion).

        Requires the axis to be zeroed; targets are checked against the
        soft limits. Both distances are loaded first, then both GO
        commands are sent back-to-back so the axes start together.
        """
        self._require_ready()
        req = []
        for value, st in ((alpha, self._alpha), (beta, self._beta)):
            if value is None:
                continue
            if not st.cfg.enabled:
                raise ValueError(f"{st.cfg.name} axis is disabled")
            if not st.cfg.zeroed:
                raise ValueError(f"{st.cfg.name} is not zeroed — absolute "
                                 f"moves disabled (jog only)")
            lo, hi = st.cfg.min_deg, st.cfg.max_deg
            if not lo <= value <= hi:
                raise ValueError(f"{st.cfg.name} target {value:+.2f}° "
                                 f"outside limits [{lo:+.1f}, {hi:+.1f}]")
            if st.moving:
                raise RuntimeError(f"{st.cfg.name} is already moving — "
                                   f"stop first")
            req.append((st, float(value)))
        with self._cmd_lock:
            started = []
            for st, value in req:
                delta = st.cfg.angle_to_counts(value) - st.counts
                if delta == 0:
                    continue
                self._proto.command(st.cfg.unit, f"D{delta}")
                started.append((st, value, delta))
            for st, value, delta in started:
                self._proto.command(st.cfg.unit, "G")
                st.target = value
                st.moving = True
                st.deadline = self._deadline_for(st, delta)
        if started:
            self._status("Moving " + " + ".join(
                f"{st.cfg.name}→{v:+.2f}°" for st, v, _ in started))

    def move_by(self, name: str, delta_deg: float) -> None:
        """Relative step move (the legacy 'Degrees per Step' buttons).

        Allowed without a zero reference; when the axis IS zeroed the
        resulting angle is checked against the soft limits first.
        """
        self._require_ready()
        st = self._axis(name)
        if not st.cfg.enabled:
            raise ValueError(f"{st.cfg.name} axis is disabled")
        if st.moving:
            raise RuntimeError(f"{st.cfg.name} is already moving")
        if st.cfg.zeroed:
            end = st.angle + delta_deg
            lo, hi = st.cfg.min_deg, st.cfg.max_deg
            if not lo <= end <= hi:
                raise ValueError(
                    f"{st.cfg.name} step to {end:+.2f}° outside limits "
                    f"[{lo:+.1f}, {hi:+.1f}]")
        else:
            self._status(f"WARNING: {st.cfg.name} not zeroed — no limit "
                         f"protection on this step")
        steps = round(st.cfg.direction * delta_deg
                      * st.cfg.steps_per_degree)
        if steps == 0:
            return
        with self._cmd_lock:
            self._proto.move_steps(st.cfg.unit, steps)
            st.target = (st.angle + delta_deg) if st.cfg.zeroed else None
            st.moving = True
            st.deadline = self._deadline_for(st, steps)
        self._status(f"{st.cfg.name} step {delta_deg:+.3f}° "
                     f"({steps:+d} steps)")

    def _deadline_for(self, st: _AxisState, steps: int) -> float:
        est = abs(steps) / (st.cfg.velocity_deg_s() *
                            st.cfg.steps_per_degree)
        return time.monotonic() + est * self.config.move_timeout_margin + 5.0

    def stop_all(self) -> None:
        """Immediate stop of both axes (E-stop path, synchronous)."""
        if self._proto is None:
            return
        for st in self._axes():
            st.moving = False
            st.target = None
        self._proto.stop_all_now([st.cfg.unit for st in self._axes()])
        self._status("STOP issued to both axes")

    def stop_axis(self, name: str) -> None:
        st = self._axis(name)
        if self._proto is None:
            return
        st.moving = False
        st.target = None
        self._proto.stop_all_now([st.cfg.unit])
        self._status(f"{st.cfg.name} stopped")

    # ── zero / fault management ──────────────────────────────────────────
    def set_current_angle(self, name: str, angle: float) -> None:
        """Declare the sting's PHYSICAL angle and zero the step counter.

        The operator confirms where the hardware actually is; the indexer
        counter is zeroed (``PZ``) and all future angles are relative to
        this reference.
        """
        self._require_ready()
        st = self._axis(name)
        if st.moving:
            raise RuntimeError("cannot zero while moving")
        with self._cmd_lock:
            self._proto.zero_position(st.cfg.unit)
            st.cfg.zero_offset_deg = float(angle)
            st.cfg.zeroed = True
            st.update_from_counts(0)
        self._save_state(clean=False)
        self._status(f"{st.cfg.name} zeroed at {angle:+.3f}°")

    def reset_fault(self) -> None:
        """Clear a latched fault (after the cause has been addressed)."""
        if self.moving:
            raise RuntimeError("stop all axes before resetting the fault")
        was = self._fault
        self._fault = None
        self._errors = 0
        self._status(f"Fault reset (was: {was})")

    def reinitialize(self, confirm_safe: bool = False) -> None:
        """Re-run the drive init sequence (includes the ``Z`` reset).

        The legacy tool warns the reset may cause uncontrolled movement if
        the sting is not in a safe position — the caller must pass
        ``confirm_safe=True`` to acknowledge.
        """
        if not confirm_safe:
            raise RuntimeError(
                "reinitialize() resets the drives; confirm the sting is "
                "in a safe position and call with confirm_safe=True")
        if not self._connected or self._proto is None:
            raise RuntimeError("connect() first")
        with self._cmd_lock:
            self._fault = None
            self._init_drives()
        for st in self._axes():
            st.cfg.zeroed = False       # counter may have been reset
        self._status("Drives reinitialized — re-zero both axes")

    # ── poll loop ────────────────────────────────────────────────────────
    def _poll_loop(self) -> None:
        period = self.config.poll_ms / 1000.0
        while not self._stop_evt.is_set():
            t0 = time.perf_counter()
            completed = []
            try:
                with self._cmd_lock:
                    if self._proto is None:
                        break
                    for st in self._axes():
                        if not st.cfg.enabled:
                            continue
                        if st.moving:
                            s = self._proto.status(st.cfg.unit)
                            if s == STALLED:
                                self._trip(
                                    f"{st.cfg.name} STALLED (*S) — "
                                    f"power-cycle the sting drives, "
                                    f"re-zero, then reset the fault")
                                break
                            if s == READY:
                                st.update_from_counts(
                                    self._proto.position(st.cfg.unit))
                                st.moving = False
                                completed.append(st.cfg.name)
                            elif time.monotonic() > st.deadline:
                                self._trip(
                                    f"{st.cfg.name} move TIMED OUT — "
                                    f"check the drive and mechanism")
                                break
                        else:
                            st.update_from_counts(
                                self._proto.position(st.cfg.unit))
                self._errors = 0
            except StingError as exc:
                self._errors += 1
                self._status(f"Serial error ({self._errors}/"
                             f"{self.config.max_consecutive_errors}): "
                             f"{exc}")
                if self._proto is not None:
                    self._proto.clear_input()
                if self._errors >= self.config.max_consecutive_errors:
                    self._watchdog_trip()
                    break

            self.ring.push_block({
                "t": np.array([time.time()]),
                "Alpha": np.array([self._alpha.angle]),
                "Beta": np.array([self._beta.angle]),
                "Alpha_steps": np.array([float(self._alpha.counts)]),
                "Beta_steps": np.array([float(self._beta.counts)]),
                "Alpha_moving": np.array([float(self._alpha.moving)]),
                "Beta_moving": np.array([float(self._beta.moving)]),
            })
            if time.monotonic() - self._last_state_save > 2.0:
                self._save_state(clean=False)
                self._last_state_save = time.monotonic()
            for name in completed:
                self._status(f"{name} move complete "
                             f"({self._axis(name).angle:+.3f}°)")
                if self.on_move_complete:
                    self.on_move_complete(name)

            elapsed = time.perf_counter() - t0
            self._stop_evt.wait(max(period - elapsed, 0.02))

    def _trip(self, message: str) -> None:
        """Latch a fault: stop everything, refuse motion until reset."""
        self._fault = message
        for st in self._axes():
            st.moving = False
            st.target = None
        if self._proto is not None:
            try:
                self._proto.stop_all_now(
                    [st.cfg.unit for st in self._axes()])
            except StingError:
                pass
        self._status(f"FAULT: {message}")

    def _watchdog_trip(self) -> None:
        self._fault = "serial watchdog — controllers unreachable"
        for st in self._axes():
            if self._proto is not None:
                try:
                    self._proto.stop_now(st.cfg.unit)
                except StingError:
                    pass
            st.moving = False
            st.target = None
        self._status("WATCHDOG: sting controllers unreachable — stopped "
                     "and holding; check power/cable, then reconnect")

    # ── helpers ──────────────────────────────────────────────────────────
    def _status(self, msg: str) -> None:
        log.info(msg)
        if self.on_status:
            self.on_status(msg)
