"""Sweep engine — the per-point state machine (spec §6).

Plain Python, no Qt: the GUI runs :meth:`SweepEngine.run` on a worker
thread and receives progress via callbacks; tests drive it directly in
sim. Per-point cycle::

    [refuse-to-record check] → set tunnel speed (one of the 3 tiers below)
    → move positioner(alpha, beta) → wait settled → (optional zero) →
    drain stale → dwell → acquire → write .h5 → advance

THREE explicit tunnel-speed control tiers (``config.tunnel_control_mode``),
each keyed off the SELECTED speed unit (``config.speed_unit``) and the
adapter's NATIVE command parameter — never forced through Mach/RPM:

* ``"manual"`` (monitor-only, the default): the "set tunnel" stage NEVER
  writes the fan — the OPERATOR brings the console to the target. With
  ``mach_check_enabled`` the engine raises an :class:`OperatorWaitRequest`
  through ``callbacks.on_operator_wait`` (proceed | skip | abort; headless
  → log + proceed); without it, the point records immediately.
* ``"auto"``: command the drive ONCE in its native kwarg (LSWT → hz= /
  velocity=, SWT → rpm=), wait the DRIVE's own setpoint-settle, record.
  No measured-flow closure, no fault on a measured mismatch.
* ``"regulate"``: auto, THEN a measured-feedback correction loop in the
  selected unit (:func:`freestream.speed.measured_value`); non-convergence
  WARNs + records by default (``tunnel_regulate_fault`` re-arms a fault).

AIR-OFF (target 0 in any unit) short-circuits every tier: manual records
immediately; auto/regulate command the fan to 0 and settle — the
measured-feedback loop NEVER runs toward 0. A run-sheet ``rpm`` override
is always commanded verbatim (open-loop), bypassing regulation.

Every wait has a timeout → the point FAULTs and the sweep pauses (spec:
recording must refuse, visibly, rather than write bad data). A failed
point keeps its run number free to be re-run individually.

PAUSE/RESUME: :meth:`SweepEngine.pause` / :meth:`SweepEngine.resume` are
thread-safe and take effect at the NEXT point boundary only — the pause
flag is checked right before starting the next point, so the current
point (including any operator Mach wait already in progress) finishes
acquiring/writing normally, then the engine HOLDS (no motion commands,
no acquisition) until resume() or abort(). Abort/E-STOP work from
paused. ``mach_check_enabled = False`` skips the per-point Mach gate
entirely (no operator dialog, no settle wait) — the sweep records each
point immediately after positioning, tunnel channels still recorded.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from . import speed
from .config import FreestreamConfig
from .derived import TUNNEL_CONDITION_CHANNELS, tunnel_state
from .hal import Positioner, SetpointDevice, Streaming, Zeroable
from .machloop import (clamp_command, command_kwarg_for, make_tunnel_measure)
from .manager import DeviceManager
from .recorder import Hdf5Recorder
from .runsheet import SweepPoint

log = logging.getLogger(__name__)

QUEUED, MOVING, ACQUIRING, DONE, FAILED, SKIPPED = (
    "queued", "moving", "acquiring", "done", "failed", "skipped")


@dataclass
class PointOutcome:
    index: int
    status: str
    path: Optional[str] = None
    error: str = ""
    t_elapsed_s: float = 0.0


#: valid answers to an OperatorWaitRequest
PROCEED, SKIP_POINT, ABORT_SWEEP = "proceed", "skip", "abort"


@dataclass
class OperatorWaitRequest:
    """Monitor-only tunnel condition: what the OPERATOR must establish.

    ``target_mach`` is set for mach points; ``target_rpm`` for run-sheet
    ``rpm`` overrides (exactly one is non-None). ``measure()`` returns
    the live ``(measured_mach, rpm_meas)`` — isentropic Mach from the
    registry's tunnel-condition streams (NaN when unavailable) +
    setpoint RPM readback; reads only, never a command. ``tolerance`` is
    the ± band on the target quantity IN the request's display unit.

    The request also speaks the configured ENTRY/DISPLAY unit
    (:mod:`freestream.speed`): ``unit`` names it, ``target_value`` is
    the target in that unit and ``measure_value()`` (velocity units)
    returns the live in-unit measurement — so the wait dialog can talk
    to the operator in the unit they planned in, while ``target_mach``
    keeps the canonical number for recording/air-state."""
    target_mach: Optional[float]
    tolerance: float
    measure: Callable[[], Tuple[float, float]]
    target_rpm: Optional[float] = None
    #: entry/display unit of this request (one of speed.SPEED_UNITS)
    unit: str = "mach"
    #: the target expressed in ``unit`` (None on plain mach requests)
    target_value: Optional[float] = None
    #: live measured speed in ``unit`` (None-returning callable when
    #: unavailable); only set for velocity/rpm display units
    measure_value: Optional[Callable[[], Optional[float]]] = None

    @property
    def is_rpm(self) -> bool:
        return self.target_mach is None

    @property
    def display_unit(self) -> str:
        """The unit the GUI should format in — rpm overrides always
        display RPM regardless of the configured entry unit."""
        return "rpm" if self.is_rpm else (self.unit or "mach")

    @property
    def display_target(self) -> Optional[float]:
        """The target number in :attr:`display_unit`."""
        if self.is_rpm:
            return self.target_rpm
        if self.display_unit == "mach":
            return self.target_mach
        return self.target_value

    def describe(self) -> str:
        if self.is_rpm:
            return f"{self.target_rpm:g} RPM"
        if self.display_unit == "mach":
            return f"Mach {self.target_mach:g}"
        return (f"{self.target_value:g} "
                f"{speed.LABELS.get(self.display_unit, self.display_unit)}")


@dataclass
class SweepCallbacks:
    """All GUI hooks; every field optional."""
    on_event: Optional[Callable[[str], None]] = None          # log line
    on_point_state: Optional[Callable[[int, str], None]] = None
    on_point_done: Optional[Callable[[PointOutcome], None]] = None
    on_finished: Optional[Callable[[List[PointOutcome]], None]] = None
    #: pause hold entered at a point boundary: (next_index, total) —
    #: the engine is holding BEFORE point next_index (0-based) of total.
    on_paused: Optional[Callable[[int, int], None]] = None
    #: resume() ended the hold; the sweep continues with the next point.
    on_resumed: Optional[Callable[[], None]] = None
    #: monitor-only tunnel prompt (control disabled). BLOCKING on the
    #: engine thread; must return "proceed" | "skip" | "abort". None →
    #: log and proceed immediately (headless default).
    on_operator_wait: Optional[
        Callable[[OperatorWaitRequest], str]] = None


class SweepAborted(RuntimeError):
    pass


class SweepEngine:
    """Runs a list of SweepPoints against the DeviceManager registry."""

    def __init__(self, manager: DeviceManager, recorder: Hdf5Recorder,
                 config: FreestreamConfig,
                 callbacks: Optional[SweepCallbacks] = None):
        self.manager = manager
        self.recorder = recorder
        self.config = config
        self.cb = callbacks or SweepCallbacks()
        self._abort = threading.Event()
        self._pause = threading.Event()      # requested (checked at boundary)
        self._holding = False                # actually holding right now
        self._running = False
        #: True once estop() (not a graceful abort()) fired — the E-STOP
        #: path is its own hard stop, so the run-end fan_stop hook is
        #: skipped for it (a normal end and a graceful abort still fire it).
        self._estopped = False
        #: run-level speed-sweep marker threaded into every point's root
        #: attrs so a single-file read shows the FULL set of setpoints —
        #: (unit, sorted-unique speed_values). None on a mach/no-speed run.
        self._run_speed_unit: Optional[str] = None
        self._run_speed_setpoints: Optional[List[float]] = None
        #: what the tunnel was actually commanded for the CURRENT point —
        #: {"rpm_cmd": float|None, "mach_cmd": float|None}; recorded into
        #: the /Tunnel group (RPM_cmd / Mach_cmd).
        self._tunnel_cmd: Dict[str, Optional[float]] = {
            "rpm_cmd": None, "mach_cmd": None}

    # ── control ──────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._running

    @property
    def abort_requested(self) -> bool:
        """True once abort()/estop() was called for the current run."""
        return self._abort.is_set()

    @property
    def pause_requested(self) -> bool:
        """True once pause() was called and resume() hasn't cleared it."""
        return self._pause.is_set()

    @property
    def paused(self) -> bool:
        """True while the engine is actually HOLDING at a point boundary."""
        return self._holding

    def pause(self) -> None:
        """Thread-safe: request a hold at the NEXT point boundary.

        The pause flag is only checked right before starting the next
        point, so the current point — including an operator Mach wait
        already in progress — finishes acquiring/writing normally, then
        the engine holds (no motion, no acquisition) until resume() or
        abort()."""
        self._pause.set()

    def resume(self) -> None:
        """Thread-safe: end a pause request / release the hold."""
        self._pause.clear()

    def abort(self) -> None:
        """Graceful stop after the current wait unwinds."""
        self._abort.set()

    def estop(self) -> None:
        """Immediate: stop all motion NOW, then abort the sweep."""
        self._abort.set()
        self._estopped = True
        self.manager.stop_all_motion()
        self._event("E-STOP: all motion stopped, sweep aborted")

    # ── main entry (call on a worker thread) ─────────────────────────────
    def run(self, points: List[SweepPoint]) -> List[PointOutcome]:
        self._abort.clear()
        self._pause.clear()
        self._estopped = False
        self._running = True
        # capture the run's full speed-setpoint set ONCE (multi-velocity
        # marker written into every point file's root attrs — Feature 2).
        self._run_speed_unit, self._run_speed_setpoints = \
            self._collect_speed_setpoints(points)
        outcomes: List[PointOutcome] = []
        try:
            # arm/run the tunnel fan ONCE before commanding any point (only
            # when an automatic tier will actually drive a non-air-off
            # speed and the adapter supports it — see the method). A failure
            # here FAULTs the sweep before point 0.
            try:
                self._prepare_tunnel_control(points)
            except Exception as exc:                   # noqa: BLE001
                log.exception("tunnel control preparation failed")
                self._event(f"point 0 FAILED — sweep paused ({exc}); "
                            f"re-run after fixing the cause")
                if points:
                    points[0].status = FAILED
                    outcomes.append(PointOutcome(0, FAILED, error=str(exc)))
                    for later in points[1:]:
                        later.status = QUEUED
                return self._finish(outcomes)
            for i, point in enumerate(points):
                # PAUSE boundary: checked right before starting the next
                # point (never mid-point) — hold until resume()/abort().
                self._hold_if_paused(i, len(points))
                if self._abort.is_set():
                    point.status = SKIPPED
                    outcomes.append(PointOutcome(i, SKIPPED))
                    continue
                outcome = self.run_point(i, point)
                outcomes.append(outcome)
                if outcome.status == FAILED:
                    self._event(f"point {i} FAILED — sweep paused "
                                f"({outcome.error}); re-run the point "
                                f"after fixing the cause")
                    for j, later in enumerate(points[i + 1:], start=i + 1):
                        later.status = QUEUED
                    break
        finally:
            self._running = False
            # finally-style hook: a normal end, a graceful abort, OR an
            # exhausted/faulted loop all shut the fan down here (E-STOP is
            # its own hard stop and is skipped — see _shutdown_tunnel_control).
            self._shutdown_tunnel_control()
        return self._finish(outcomes)

    @staticmethod
    def _collect_speed_setpoints(points: List[SweepPoint]):
        """The run's (unit, sorted-unique speed_values) across every point
        that carries a NON-mach ``speed_unit`` + ``speed_value`` (the
        planner stamps these for Hz/ft·s/m·s/RPM sweeps). Returns
        ``(None, None)`` for a mach / no-speed run so nothing spurious is
        written."""
        unit: Optional[str] = None
        values: set = set()
        for p in points:
            u = p.meta.get("speed_unit")
            sv = p.meta.get("speed_value")
            if u and str(u).lower() not in ("mach", "none") and sv is not None:
                unit = str(u)
                try:
                    values.add(float(sv))
                except (TypeError, ValueError):
                    continue
        if not values:
            return None, None
        return unit, sorted(values)

    def _finish(self, outcomes: List[PointOutcome]) -> List[PointOutcome]:
        self._running = False
        if self.cb.on_finished:
            self.cb.on_finished(outcomes)
        return outcomes

    # ── tunnel-fan arming (auto/regulate velocity sweeps) ────────────────
    def _prepare_tunnel_control(self, points: List[SweepPoint]) -> None:
        """Once, before the point loop commands the tunnel: on an AUTOMATIC
        tier (``tunnel_control_mode`` in {"auto","regulate"}) with at least
        one non-air-off speed point, arm+run the fan if the adapter supports
        it (LSWT ACS530). The ABB drive only ramps its reference while the
        fan is RUNNING (START word), so an automatic Hz/velocity sweep would
        otherwise command a reference the fan never chases → at_target never
        true → timeout. Capability-gated (NOT mode-name-gated): the SWT Red
        Lion fan has no ``fan_start`` (operator/console-run) and is a clean
        no-op here, as is manual mode and an all-air-off sweep."""
        if self._control_mode() not in ("auto", "regulate"):
            return                                     # manual never writes
        if not self._has_running_fan_point(points):
            return                                     # all air-off: no fan
        dev = self.manager.setpoint
        if dev is None:
            return
        fan_start = getattr(dev, "fan_start", None)
        if not callable(fan_start):
            return             # SWT Red Lion etc. — operator-run fan, no-op
        running = self._fan_running(dev)
        if running is True:
            self._event("tunnel: fan armed and running (auto control)")
            return
        fan_start()
        if running is None:
            # no running indicator to confirm against — a successful
            # fan_start is treated as sufficient (idempotent in SIM)
            self._event("tunnel: fan armed and running (auto control)")
            return
        try:
            self._wait(lambda: self._fan_running(dev) is True,
                       self.config.tunnel_timeout_s,
                       "fan never reported running")
        except TimeoutError:
            raise RuntimeError(
                "tunnel fan did not start/arm — enable fan control and "
                "start the fan (LSWT ACS530) before an automatic velocity "
                "sweep")
        self._event("tunnel: fan armed and running (auto control)")

    def _shutdown_tunnel_control(self) -> None:
        """Once, AFTER the point loop ends (success, abort, or exhausted):
        on an AUTOMATIC tier (``tunnel_control_mode`` in {"auto","regulate"})
        stop the tunnel fan if the adapter supports it (LSWT ACS530
        ``fan_stop`` = STOP word + zero reference), so an automatic run
        leaves the tunnel shut down. Capability-gated (NOT mode-name-gated):
        the SWT Red Lion has no ``fan_stop`` (operator/console-run fan) and
        is a clean no-op, as is manual mode. Skipped after an E-STOP — that
        path is its own hard stop; a normal end and a graceful abort BOTH
        fire it exactly once (called from the run() finally)."""
        if self._estopped:
            return                                     # E-STOP handles itself
        if self._control_mode() not in ("auto", "regulate"):
            return                                     # manual never stops fan
        dev = self.manager.setpoint
        if dev is None:
            return
        fan_stop = getattr(dev, "fan_stop", None)
        if not callable(fan_stop):
            return             # SWT Red Lion etc. — operator-run fan, no-op
        try:
            fan_stop()
        except Exception as exc:                       # noqa: BLE001
            log.exception("tunnel fan stop failed")
            self._event(f"tunnel: fan stop failed at run end ({exc})")
            return
        self._event("tunnel: fan stopped (run complete)")

    def _has_running_fan_point(self, points: List[SweepPoint]) -> bool:
        """True when at least one point commands a NON-air-off tunnel speed
        (a canonical Mach / RPM override / entered value > 0) — an all
        air-off sweep needs no running fan."""
        for p in points:
            rpm_override = p.meta.get("rpm")
            if rpm_override is None and p.mach is None:
                continue                               # commands no speed
            if not self._is_air_off(p, rpm_override):
                return True
        return False

    @staticmethod
    def _fan_running(dev: SetpointDevice) -> Optional[bool]:
        """The running indicator: ``snapshot().fan_running`` when available,
        else ``state()["running"]``, else None (no indicator — the caller
        treats a successful ``fan_start`` as sufficient)."""
        snap = getattr(dev, "snapshot", None)
        if callable(snap):
            try:
                s = snap()
            except Exception:                          # noqa: BLE001
                s = None
            if s is not None and hasattr(s, "fan_running"):
                return bool(s.fan_running)
        state = getattr(dev, "state", None)
        if callable(state):
            try:
                st = state()
            except Exception:                          # noqa: BLE001
                st = None
            if isinstance(st, dict) and "running" in st:
                return bool(st["running"])
        return None

    def _hold_if_paused(self, index: int, total: int) -> None:
        """Point-boundary pause hold: while pause is requested (and no
        abort), sit here issuing NO motion commands and NO acquisition.
        Runs on the engine worker thread."""
        if not self._pause.is_set() or self._abort.is_set():
            return
        self._holding = True
        try:
            self._event(f"sweep PAUSED — holding before point "
                        f"{index + 1}/{total}")
            if self.cb.on_paused:
                self.cb.on_paused(index, total)
            while self._pause.is_set() and not self._abort.is_set():
                time.sleep(0.05)
        finally:
            self._holding = False
        if not self._abort.is_set():
            self._event(f"sweep RESUMED — continuing with point "
                        f"{index + 1}/{total}")
            if self.cb.on_resumed:
                self.cb.on_resumed()

    # ── one point (also used for individual re-runs) ────────────────────
    def run_point(self, index: int, point: SweepPoint) -> PointOutcome:
        t0 = time.perf_counter()
        try:
            self._check_blockers()
            self._require_data_devices()
            self._set_tunnel(point)
            self._move(point)
            self._zero_if_wanted(point)
            path = self._acquire_and_write(point)
            point.status = DONE
            outcome = PointOutcome(index, DONE, path=str(path),
                                   t_elapsed_s=time.perf_counter() - t0)
            # `path` IS the written primary file (extension per the
            # recorder's output_format), so this line is format-agnostic
            self._event(f"point {index} done → {path}")
        except SweepAborted:
            point.status = SKIPPED
            outcome = PointOutcome(index, SKIPPED,
                                   t_elapsed_s=time.perf_counter() - t0)
        except Exception as exc:                       # noqa: BLE001
            log.exception("point %d failed", index)
            point.status = FAILED
            outcome = PointOutcome(index, FAILED, error=str(exc),
                                   t_elapsed_s=time.perf_counter() - t0)
        if self.cb.on_point_done:
            self.cb.on_point_done(outcome)
        return outcome

    #: message when an automated sweep is asked of a device set with no
    #: data-acquisition (Streaming) devices — e.g. Mode 3 traverse-only.
    NO_DATA_DEVICES_MSG = (
        "no data devices to record — cannot run an automated sweep in "
        "this device set (e.g. traverse-only mode: use the embedded "
        "traverse panel for manual positioning)")

    # ── cycle stages ─────────────────────────────────────────────────────
    def _require_data_devices(self) -> None:
        """Fail fast (before any tunnel/motion command) if there is nothing
        to record. The GUI already blocks Start for such a set (Task 1);
        this is the belt-and-braces guard if run_point is reached anyway."""
        if not self.manager.streaming:
            raise RuntimeError(self.NO_DATA_DEVICES_MSG)

    def _check_blockers(self) -> None:
        if not self.config.refuse_on_blockers:
            return
        blockers = self.manager.record_blockers()
        if blockers:
            raise RuntimeError("REFUSING TO RECORD — " +
                               "; ".join(blockers))

    # ── tunnel-speed control (3 tiers: manual / auto / regulate) ─────────
    def _control_mode(self) -> str:
        """The configured control tier, resolved for back-compat: an
        explicit ``tunnel_control_mode`` wins; otherwise the legacy
        ``tunnel_control_enabled`` boolean (True → regulate, False →
        manual). ``FreestreamConfig.__post_init__`` already resolves this,
        but the fallback keeps hand-built configs sane."""
        mode = str(getattr(self.config, "tunnel_control_mode", "") or "")
        mode = mode.strip().lower()
        if mode in ("manual", "auto", "regulate"):
            return mode
        return "regulate" if self.config.tunnel_control_enabled else "manual"

    def _speed_unit(self) -> str:
        unit = getattr(self.config, "speed_unit", "mach")
        return unit if unit in speed.SPEED_UNITS else "mach"

    @staticmethod
    def _is_air_off(point: SweepPoint, rpm_override) -> bool:
        """A target of 0 in ANY unit = air off (Mach 0 ⟺ 0 in every unit).
        Checked across the canonical Mach, a run-sheet RPM override and the
        planner's entered speed value so the short-circuit fires regardless
        of which the point carries."""
        if point.mach is not None and abs(float(point.mach)) < 1e-6:
            return True
        if rpm_override is not None and abs(float(rpm_override)) < 1e-6:
            return True
        sv = point.meta.get("speed_value")
        if sv is not None and abs(float(sv)) < 1e-6:
            return True
        return False

    def _record_requested(self, point: SweepPoint, rpm_override) -> None:
        """Stamp /Tunnel Mach_cmd/RPM_cmd from the REQUESTED target (no
        write). An RPM override records RPM_cmd only (no Mach was
        commanded), mirroring the historical monitor-only behavior."""
        target_rpm = None if rpm_override is None else float(rpm_override)
        target_mach = (float(point.mach)
                       if rpm_override is None and point.mach is not None
                       else None)
        self._tunnel_cmd = {"rpm_cmd": target_rpm, "mach_cmd": target_mach}

    def _set_tunnel(self, point: SweepPoint) -> None:
        self._tunnel_cmd = {"rpm_cmd": None, "mach_cmd": None}
        rpm_override = point.meta.get("rpm")
        if rpm_override is None and point.mach is None:
            return
        dev = self.manager.setpoint
        if dev is None:
            raise RuntimeError(
                "point requests a tunnel condition (Mach/RPM) but this "
                "device set has no tunnel SetpointDevice — add a tunnel "
                "device or remove the Mach/RPM column from these points")
        mode = self._control_mode()
        if mode == "manual":
            self._tunnel_manual(point, rpm_override, dev)
        else:
            self._tunnel_command(point, rpm_override, dev,
                                 regulate=(mode == "regulate"))

    # ── Tier A: MANUAL (monitor-only, never writes the fan) ──────────────
    def _tunnel_manual(self, point: SweepPoint, rpm_override,
                       dev: SetpointDevice) -> None:
        """The OPERATOR brings the tunnel to the target; Freestream only
        monitors. ``mach_check_enabled`` is the sub-toggle: True = wait
        (operator dialog) until the measured value holds in tolerance, then
        auto-proceed; False = record immediately. Air-off (target 0)
        records immediately either way — there is nothing to wait for."""
        if self._is_air_off(point, rpm_override):
            self._record_requested(point, rpm_override)
            self._event("tunnel: air-off (target 0) — recording "
                        "immediately (manual, no verify wait)")
            return
        if not self.config.mach_check_enabled:
            # per-point speed gate DISABLED: no dialog, no settle wait —
            # record immediately with the requested condition in /Tunnel.
            self._record_requested(point, rpm_override)
            self._event(
                "tunnel: Mach verification disabled — recording "
                "immediately without waiting for tunnel conditions")
            return
        # verify-and-wait: the OPERATOR sets the console; we wait/prompt.
        self._operator_wait(point, rpm_override, dev)

    # ── Tiers B/C: AUTO (open-loop) + REGULATE (closed-loop to tol) ──────
    def _tunnel_command(self, point: SweepPoint, rpm_override,
                        dev: SetpointDevice, regulate: bool) -> None:
        """Command the tunnel in the adapter's NATIVE parameter (never
        forced through Mach/RPM). AUTO commands once and waits the DRIVE's
        own setpoint-settle; REGULATE then adds a measured-feedback
        correction loop in the SELECTED speed unit."""
        # AIR-OFF short-circuit (all command tiers): command the drive to
        # 0 (fan off), wait it to settle, record — NEVER run the measured
        # loop toward 0 (you cannot regulate a garbage measured Mach down
        # to 0 by nudging an already-off fan; that is exactly Casey's
        # fault).
        if self._is_air_off(point, rpm_override):
            self._command_air_off(point, rpm_override, dev)
            return
        if rpm_override is not None:
            # documented direct-RPM override (run-sheet "rpm" column) —
            # commands the fan verbatim, open-loop, BYPASSING regulation.
            rpm = float(rpm_override)
            self._event(f"tunnel → {rpm:g} RPM (direct override, "
                        f"Mach loop bypassed)")
            dev.set_target(rpm=rpm)
            self._wait(dev.at_target, self.config.tunnel_timeout_s,
                       f"tunnel never reached {rpm:g} RPM")
            self._tunnel_cmd = {"rpm_cmd": rpm, "mach_cmd": None}
            return
        unit = self._speed_unit()
        entered = point.meta.get("speed_value")
        kwarg, value = command_kwarg_for(dev, float(point.mach), entered,
                                         unit, self.config)
        value = clamp_command(dev, kwarg, value)
        # Tier B: command once, wait the drive's OWN setpoint-settle
        self._event(f"tunnel → {value:g} {kwarg} (auto command "
                    f"[{unit}], canonical Mach {float(point.mach):g})")
        dev.set_target(**{kwarg: value})
        self._wait(dev.at_target, self.config.tunnel_timeout_s,
                   f"tunnel never settled at {value:g} {kwarg}")
        value = self._record_command(point, kwarg, value)
        if not regulate:
            return
        # Tier C: closed-loop correction in the SELECTED unit
        if self._sim_measurement(dev):
            # sim plant: the sim DAQ pressures do not respond to the fan,
            # so measured-feedback can't close — degrade to the open-loop
            # command (clearly logged), never fault.
            self._event("tunnel: sim plant — measured-feedback regulation "
                        "skipped (open-loop command holds)")
            return
        self._regulate(point, dev, unit, kwarg, value)

    def _command_air_off(self, point: SweepPoint, rpm_override,
                         dev: SetpointDevice) -> None:
        """Air-off in a command tier: drive the fan to 0 and wait it to
        settle at 0 — no measured-feedback loop toward 0."""
        kwarg = "rpm"
        rb = {}
        try:
            rb = dev.readback() or {}
        except Exception:                              # noqa: BLE001
            rb = {}
        if ("hz" in rb) or ("velocity_fps" in rb):
            kwarg = "hz"                               # LSWT drive: 0 Hz
        self._event(f"tunnel: air-off — commanding {kwarg}=0 (fan off), "
                    f"no measured-feedback toward 0")
        try:
            dev.set_target(**{kwarg: 0.0})
        except Exception as exc:                       # noqa: BLE001
            self._event(f"tunnel: air-off command warning ({exc}); "
                        "recording air-off regardless")
        else:
            self._wait(dev.at_target, self.config.tunnel_timeout_s,
                       "tunnel never settled at 0 (air off)")
        self._record_requested(point, rpm_override)

    def _record_command(self, point: SweepPoint, kwarg: str,
                         value: float):
        """Record the commanded NATIVE value + the canonical Mach into
        /Tunnel. Hz/RPM commands land in RPM_cmd (Hz≡RPM 1:1 on the LSWT
        drive); a velocity command leaves RPM_cmd to the readback. Returns
        the value actually stored (unchanged) for the regulate loop."""
        rpm_cmd = value if kwarg in ("rpm", "hz") else None
        mach_cmd = float(point.mach) if point.mach is not None else None
        self._tunnel_cmd = {"rpm_cmd": rpm_cmd, "mach_cmd": mach_cmd}
        return value

    def _sim_measurement(self, dev: SetpointDevice) -> bool:
        """True when measured-feedback closure is impossible because the
        drive or the tunnel-condition source(s) are simulated (the sim
        plant's pressures don't respond to the fan)."""
        if getattr(dev, "sim", True):
            return True
        for s in getattr(self.manager, "streaming", []):
            names = set()
            try:
                names = {ch.name for ch in s.channels()}
            except Exception:                          # noqa: BLE001
                continue
            if "Pdiff" in names and getattr(s, "sim", True):
                return True
        return False

    def _regulate(self, point: SweepPoint, dev: SetpointDevice, unit: str,
                  kwarg: str, value: float) -> None:
        """Measured-feedback correction IN THE SELECTED UNIT: measure via
        speed.measured_value; within speed_tolerance → done; else nudge the
        NATIVE command proportionally (clamped to the adapter limit) and
        retry up to mach_max_iterations. On non-convergence DO NOT fault by
        default — WARN and RECORD the best command (config
        ``tunnel_regulate_fault`` re-arms the historical hard fault)."""
        target = speed.value_from_mach(float(point.mach), unit,
                                       self.config.rpm_per_mach)
        tol = float(self.config.speed_tolerance)
        max_iter = max(int(self.config.mach_max_iterations), 1)
        timeout = float(self.config.tunnel_timeout_s)
        label = speed.LABELS.get(unit, unit)
        meas = speed.measured_value(self.manager, dev, unit)
        for i in range(1, max_iter + 1):
            if meas is None:
                # no measurement (open/garbage channel) → can't regulate;
                # non-fatal by default (this is Casey's open-Pdiff case)
                return self._regulate_giveup(
                    f"no measured {label} available", dev, kwarg, value)
            if abs(meas - target) <= tol:
                self._event(f"tunnel regulate: at target — measured "
                            f"{meas:g} {label} (target {target:g} ± "
                            f"{tol:g}) after {i} command(s)")
                return
            if i >= max_iter:
                break
            # proportional nudge in the native parameter (clamped)
            if meas > 1e-9 and value > 0:
                nudged = value * (target / meas)
            elif value > 0:
                nudged = value * 1.25          # flow not established: step up
            else:
                nudged = value
            nudged = clamp_command(dev, kwarg, nudged)
            self._event(f"tunnel regulate: measured {meas:g} {label} off "
                        f"target {target:g} — correcting to {nudged:g} "
                        f"{kwarg} (iteration {i + 1}/{max_iter})")
            value = nudged
            dev.set_target(**{kwarg: value})
            self._wait(dev.at_target, timeout,
                       f"tunnel never settled at {value:g} {kwarg}")
            self._record_command(point, kwarg, value)
            meas = speed.measured_value(self.manager, dev, unit)
        self._regulate_giveup(
            f"measured {meas:g} {label} not within ±{tol:g} of target "
            f"{target:g} after {max_iter} command(s)", dev, kwarg, value)

    def _regulate_giveup(self, why: str, dev: SetpointDevice, kwarg: str,
                         value: float) -> None:
        """Non-convergence policy: raise the historical hard FAULT only
        when ``tunnel_regulate_fault`` is set; otherwise WARN and keep the
        best command (the point still records honestly)."""
        if getattr(self.config, "tunnel_regulate_fault", False):
            raise RuntimeError(
                f"tunnel regulate FAULT: {why} — check the tunnel-condition "
                f"channels and the drive; set tunnel_regulate_fault=False "
                f"to record anyway")
        self._event(f"WARNING: tunnel regulate did not converge — {why}; "
                    f"recording with the best command ({value:g} {kwarg}) "
                    f"— regulation is advisory (tunnel_regulate_fault off)")

    def _operator_wait(self, point: SweepPoint, rpm_override,
                       dev: SetpointDevice) -> None:
        """Monitor-only tunnel stage: prompt the operator (or, headless,
        log + proceed) instead of commanding the fan. NO writes here —
        only DAQ/readback reads via the request's measure()/
        measure_value().

        The request speaks the CONFIGURED entry unit (freestream.speed):
        mach points under the mach unit keep the historical Mach band
        exactly; rpm-unit GRID points carry the configured
        speed_tolerance while run-sheet rpm overrides under any other
        unit keep the legacy ±1 % (≥1 RPM) band (old sheets unchanged);
        velocity/rpm entry units get the planner's entered target plus
        a LIVE in-unit measure so "at target" is judged honestly."""
        cfg = self.config
        unit = getattr(cfg, "speed_unit", "mach")
        if unit not in speed.SPEED_UNITS:
            unit = "mach"
        measure = make_tunnel_measure(self.manager, dev)
        if rpm_override is not None:
            target_rpm = float(rpm_override)
            if unit == "rpm" and point.meta.get("speed_unit") == "rpm":
                tol = float(cfg.speed_tolerance)   # rpm-unit grid band
            else:
                tol = max(abs(target_rpm) * 0.01, 1.0)  # legacy ±1 %/≥1
            req = OperatorWaitRequest(
                target_mach=None, tolerance=tol, measure=measure,
                target_rpm=target_rpm, unit="rpm",
                target_value=target_rpm)
        elif unit == "mach":
            # byte-for-byte the historical mach-point behavior
            req = OperatorWaitRequest(
                target_mach=float(point.mach),
                tolerance=float(cfg.mach_tolerance), measure=measure)
        else:
            # velocity (or rpm-display) entry unit: entered value when
            # the planner stamped one, else the canonical Mach through
            # the NOMINAL map — a run-sheet mach point still gets an
            # honest in-unit target
            target_mach = float(point.mach)
            entered = point.meta.get("speed_value")
            target_value = (float(entered) if entered is not None else
                            speed.value_from_mach(target_mach, unit,
                                                  cfg.rpm_per_mach))
            manager = self.manager
            req = OperatorWaitRequest(
                target_mach=target_mach,
                tolerance=float(cfg.speed_tolerance), measure=measure,
                unit=unit, target_value=target_value,
                measure_value=lambda: speed.measured_value(manager, dev,
                                                           unit))
        target_mach = req.target_mach
        target_rpm = req.target_rpm
        if self._abort.is_set():
            raise SweepAborted()
        if self.cb.on_operator_wait is None:
            mach_now, rpm_now = req.measure()
            self._event(
                f"tunnel MONITOR-ONLY (control disabled — Red Lion Block2 "
                f"writes rejected): no operator prompt registered — "
                f"proceeding immediately at target {req.describe()} "
                f"(measured Mach {mach_now:.3f}, {rpm_now:g} RPM)")
            decision = PROCEED
        else:
            self._event(
                f"tunnel MONITOR-ONLY (control disabled — Red Lion Block2 "
                f"writes rejected): waiting for the operator to bring the "
                f"tunnel to {req.describe()}")
            decision = self.cb.on_operator_wait(req)
        if self._abort.is_set():                       # E-STOP/Abort during
            raise SweepAborted()                       # the wait wins
        if decision == PROCEED:
            self._event(f"operator wait: proceed — recording point at "
                        f"{req.describe()}")
            self._tunnel_cmd = {"rpm_cmd": target_rpm,
                                "mach_cmd": target_mach}
            return
        if decision == SKIP_POINT:
            self._event(f"operator wait: point at {req.describe()} "
                        f"SKIPPED by operator")
            raise SweepAborted()                       # this point only
        # "abort" (or anything unrecognised — fail safe): whole sweep
        self._event(f"operator wait: sweep ABORTED by operator "
                    f"(at {req.describe()}, decision {decision!r})")
        self.abort()
        raise SweepAborted()

    def _move(self, point: SweepPoint) -> None:
        axes = {k: v for k, v in (("alpha", point.alpha),
                                  ("beta", point.beta),
                                  ("x", point.x),
                                  ("y", point.y),
                                  ("z", point.z)) if v is not None}
        if not axes:
            return
        pos = self.manager.positioner
        if pos is None:
            raise RuntimeError("point requests motion but no Positioner "
                               "is registered")
        # Only command axes the positioner actually exposes: alpha/beta
        # drive the sting rigs (crescent/ate), x/y/z drive the traverse
        # (Mode 3 matrix sweeps). A point carrying axes the active
        # positioner doesn't have simply doesn't command them (harmless —
        # e.g. an alpha/beta run sheet loaded while the traverse is the
        # positioner moves nothing). EXCEPTION: a dropped beta on an
        # attitude rig is called out VISIBLY — a ½-span ATE
        # (span_config="half") has no beta axis, and the operator must
        # see that the run sheet's beta column is not being flown.
        valid = {a.name for a in pos.axes()}
        dropped = {k: v for k, v in axes.items() if k not in valid}
        axes = {k: v for k, v in axes.items() if k in valid}
        if "beta" in dropped and "alpha" in valid:
            span = getattr(pos, "span_config", "")
            self._event(
                f"WARNING: beta={dropped['beta']:g} dropped — positioner "
                f"'{getattr(pos, 'id', '?')}' has no beta axis"
                + (" (½-span configuration)" if span == "half" else ""))
        if not axes:
            return
        self._set_point_state(point, MOVING)
        self._event("move → " + ", ".join(f"{k}={v:g}"
                                          for k, v in axes.items()))
        pos.move_to(**axes)
        time.sleep(self.config.settle_poll_s)       # let motion begin
        self._wait(pos.settled, self.config.move_timeout_s,
                   "positioner never settled")

    def _zero_if_wanted(self, point: SweepPoint) -> None:
        if not self.config.zero_each_point:
            return
        for dev in self.manager.zeroables:
            self._event(f"zero {getattr(dev, 'id', '?')}")
            dev.zero()

    def _acquire_and_write(self, point: SweepPoint):
        self._set_point_state(point, ACQUIRING)
        streams: List[Streaming] = self.manager.streaming
        if not streams:
            raise RuntimeError(self.NO_DATA_DEVICES_MSG)
        for s in streams:
            s.drain_block()                          # flush stale samples
        if point.dwell_s > 0:
            self._sleep_abortable(point.dwell_s)

        rates = {}
        for s in streams:
            for ch in s.channels():
                rates[ch.group] = s.sample_rate()
        primary_rate = max(rates.values())
        acquire_s = max(point.samples / primary_rate, 0.2)

        # sample positioner + tunnel at ~10 Hz during the acquisition
        pos = self.manager.positioner
        setpoint = self.manager.setpoint
        pos_samples: Dict[str, list] = {}
        tun_samples: Dict[str, list] = {}
        t_start = time.time()
        t_end = time.perf_counter() + acquire_s
        while time.perf_counter() < t_end:
            if self._abort.is_set():
                raise SweepAborted()
            if pos is not None:
                for name, value in pos.positions().items():
                    pos_samples.setdefault(name.capitalize(),
                                           []).append(value)
            if setpoint is not None:
                try:
                    rb = setpoint.readback()
                except Exception:                      # noqa: BLE001
                    rb = {}
                rpm_cmd = self._tunnel_cmd.get("rpm_cmd")
                if "rpm" in rb:
                    # classic RPM tunnel (SWT PLC): the historical
                    # RPM_meas/RPM_cmd channel pair, unchanged
                    tun_samples.setdefault("RPM_meas", []).append(
                        rb.get("rpm", 0.0))
                    tun_samples.setdefault("RPM_cmd", []).append(
                        rpm_cmd if rpm_cmd is not None
                        else rb.get("rpm_set", 0.0))
                else:
                    # generic SetpointDevice (e.g. the LSWT fan drive):
                    # record EVERY numeric readback key honestly —
                    # "<key>_meas", with a trailing "_set" mapped to
                    # "<key>_cmd" (the commanded value)
                    for key, val in rb.items():
                        try:
                            fval = float(val)
                        except (TypeError, ValueError):
                            continue
                        name = (f"{key[:-4]}_cmd" if key.endswith("_set")
                                else f"{key}_meas")
                        tun_samples.setdefault(name, []).append(fval)
            time.sleep(0.1)

        blocks: Dict[str, Dict[str, np.ndarray]] = {}
        units: Dict[str, Dict[str, str]] = {}
        want = int(point.samples)                    # exact requested count
        short: Dict[str, int] = {}                   # groups that came up short
        for s in streams:
            drained = s.drain_block()
            for ch in s.channels():
                if ch.name not in drained:
                    continue
                arr = np.asarray(drained[ch.name], dtype=np.float64)
                if want > 0:
                    if arr.size > want:
                        # keep the MOST-RECENT `want` samples (steady state)
                        arr = arr[-want:]
                    elif arr.size < want:
                        short[ch.group] = min(short.get(ch.group, arr.size),
                                              arr.size)
                blocks.setdefault(ch.group, {})[ch.name] = arr
                units.setdefault(ch.group, {})[ch.name] = ch.unit
        for group, got in short.items():
            self._event(f"WARNING: {group} captured {got} samples < "
                        f"requested {want} — writing all {got} available "
                        f"(no trim)")
        # /Positioner source of truth: when the ACTIVE positioner itself
        # STREAMS a Positioner group (Mode 2: the ATE samples its cached
        # alpha/beta per load frame), those device-truth samples are
        # authoritative and must never be clobbered by the engine's ~10 Hz
        # poll block (the historical overwrite here replaced the ATE's
        # streamed positions wholesale). The poll loop above still runs —
        # it is what refreshes the ATE's position cache during the point —
        # but its samples are only written when nothing streamed.
        streamed_pos = bool(blocks.get("Positioner"))
        if pos_samples and not streamed_pos:
            blocks["Positioner"] = {k: np.asarray(v) for k, v in
                                    pos_samples.items()}
            rates["Positioner"] = 10.0
        if blocks.get("Positioner"):
            # ½-span rig (e.g. ATE span_config="half"): there is no beta
            # axis, so no Beta samples — but Streamlined's reader/
            # resampler expects the Alpha/Beta channel pair on attitude
            # rigs, so record an honest constant-zero Beta (the semispan
            # model has no sideslip axis in this configuration; the
            # span_config root attr says why).
            if ("Alpha" in blocks["Positioner"]
                    and "Beta" not in blocks["Positioner"]):
                n = len(blocks["Positioner"]["Alpha"])
                blocks["Positioner"]["Beta"] = np.zeros(n)
            # per-axis units from the positioner's own axis specs
            # (crescent/ate report deg; the traverse reports inches)
            axis_units = {a.name.capitalize(): a.unit for a in pos.axes()} \
                if pos is not None else {}
            units["Positioner"] = {k: axis_units.get(k, "deg")
                                   for k in blocks["Positioner"]}
        if tun_samples:
            blocks["Tunnel"] = {k: np.asarray(v) for k, v in
                                tun_samples.items()}
            units["Tunnel"] = {k: ("RPM" if k.startswith("RPM") else "-")
                               for k in tun_samples}
            mach_cmd = self._tunnel_cmd.get("mach_cmd")
            if mach_cmd is not None:                 # constant Mach_cmd
                n = len(next(iter(blocks["Tunnel"].values())))
                blocks["Tunnel"]["Mach_cmd"] = np.full(n, float(mach_cmd))
                units["Tunnel"]["Mach_cmd"] = "-"
            rates["Tunnel"] = 10.0
            self._add_derived_tunnel(blocks, units)

        empty = [g for g, chans in blocks.items()
                 if any(len(a) == 0 for a in chans.values())]
        if empty:
            raise RuntimeError(f"acquisition produced empty channels in "
                               f"{empty} — refusing to write")

        meta = {"alpha": point.alpha, "beta": point.beta,
                "mach": point.mach, "x": point.x, "y": point.y,
                "z": point.z, "t_start": t_start, **point.meta}
        # Air state is DERIVED from Mach when the point has one: Mach 0 (a
        # tare point) is air-off, any positive Mach is air-on. A no-tunnel
        # sweep (mach is None) falls back to the run-sheet air_state column.
        if point.mach is not None:
            air_state = "AirOff" if abs(point.mach) < 1e-6 else "AirOn"
        else:
            air_state = point.air_state
        extra_attrs = {"mode": self.manager.mode,
                       "operator": self.config.operator,
                       "config_name": self.config.config_name}
        # self-describing file markers (device-agnostic — derived from the
        # ROLE adapters, never hardcoded per mode): which device produced
        # /Positioner, and which group/kind of balance produced the loads.
        extra_attrs.update(self._source_markers(pos))
        # positioner-declared model span ("full" | "half") — inherited
        # into the file's ROOT attrs so post-processing (Streamlined) can
        # interpret the Positioner channels (½-span: Alpha is yaw-derived
        # and Beta is a recorded constant zero)
        span = getattr(pos, "span_config", None)
        if isinstance(span, str) and span:
            extra_attrs["span_config"] = span
        # run-level speed-sweep marker (Feature 2): the FULL set of speed
        # setpoints + the entry unit, so a single-file read shows the run
        # swept multiple velocities. Per-point speed_unit/speed_value already
        # ride point.meta → root attrs; this adds the run-wide list. Only on
        # a non-mach speed sweep — nothing spurious on a mach/no-speed run.
        if self._run_speed_setpoints:
            extra_attrs["speed_setpoints"] = list(self._run_speed_setpoints)
            if self._run_speed_unit:
                extra_attrs["speed_unit"] = self._run_speed_unit
        return self.recorder.write_point(
            point_meta={k: v for k, v in meta.items() if v is not None},
            blocks=blocks, rates=rates, channel_units=units,
            air_state=air_state,
            extra_attrs=extra_attrs,
            device_meta=[self._device_meta(d_id, dev)
                         for d_id, dev in self.manager.devices.items()],
            config_snapshot=self.config.to_dict())

    def _source_markers(self, pos) -> Dict[str, str]:
        """Root-attr markers that make the file self-describing:

        * ``positions_source`` — id of the Positioner-role adapter that
          produced /Positioner ("ate" | "crescent" | "traverse" | …);
        * ``balance_group`` — HDF5 group the balance-role adapter records
          its load channels under ("ATE_Balance", "StrainBook_0", …);
        * ``balance_type`` — "external" | "internal", from the balance
          adapter's ``extra_meta()``/``balance_type`` attr.

        Everything is derived generically from the role adapters so new
        devices/modes are covered without touching this code."""
        markers: Dict[str, str] = {}
        if pos is not None:
            src = getattr(pos, "id", "")
            if src:
                markers["positions_source"] = str(src)
        bal = self.manager.by_role("balance")
        if bal is None:
            return markers
        try:
            groups = [ch.group for ch in bal.channels()
                      if getattr(ch, "kind", "") != "position" and ch.group]
        except Exception:                              # noqa: BLE001
            groups = []
        if groups:
            markers["balance_group"] = str(groups[0])
        extra = getattr(bal, "extra_meta", None)
        meta = {}
        if callable(extra):
            try:
                meta = extra() or {}
            except Exception:                          # noqa: BLE001
                meta = {}
        btype = meta.get("balance_type") or getattr(bal, "balance_type", "")
        if btype:
            markers["balance_type"] = str(btype)
        return markers

    def _device_meta(self, d_id: str, dev) -> Dict:
        """One /meta/devices entry: the fixed keys plus whatever extra
        metadata the adapter itself declares via ``extra_meta()`` (the
        ATE contributes its span_config; the mechanism is generic)."""
        meta = {"id": d_id, "sim": dev.sim,
                "cal_file": self.config.cal_files.get(d_id, "")}
        extra = getattr(dev, "extra_meta", None)
        if callable(extra):
            try:
                meta.update(extra() or {})
            except Exception:                          # noqa: BLE001
                log.exception("extra_meta() failed on %s", d_id)
        return meta

    def _add_derived_tunnel(self, blocks, units) -> None:
        """Mach/q convenience channels (kind=derived; raw stays raw).

        Uses the ONE isentropic chain in :mod:`freestream.derived` (the
        Streamlined SSWT formulas), so Mach_meas/q_meas here agree with
        the live monitors and the MachLoop number-for-number. The
        Pdiff/Ptot/Temp channels are found BY NAME across the recorded
        raw groups — all three in the SWT DaqBook group, or split across
        devices (LSWT: Pdiff with the NI DAQ, Ptot/Temp with the Heise).
        Missing any of the three → no derived channels, as before."""
        means: Dict[str, float] = {}
        for name in TUNNEL_CONDITION_CHANNELS:
            for group, chans in blocks.items():
                if group == "Tunnel":                # engine-written group
                    continue
                arr = chans.get(name)
                if arr is not None and len(arr):
                    means[name] = float(np.mean(arr))
                    break
        if any(k not in means for k in TUNNEL_CONDITION_CHANNELS):
            return
        st = tunnel_state(means["Pdiff"], means["Ptot"], means["Temp"])
        if st.valid:
            n = len(next(iter(blocks["Tunnel"].values())))
            blocks["Tunnel"]["Mach_meas"] = np.full(n, st.mach)
            blocks["Tunnel"]["q_meas"] = np.full(n, st.q_psi)
            units["Tunnel"]["Mach_meas"] = "-"
            units["Tunnel"]["q_meas"] = "psi"

    # ── helpers ──────────────────────────────────────────────────────────
    def _wait(self, cond: Callable[[], bool], timeout_s: float,
              fail_msg: str) -> None:
        deadline = time.perf_counter() + timeout_s
        while time.perf_counter() < deadline:
            if self._abort.is_set():
                raise SweepAborted()
            if cond():
                return
            time.sleep(self.config.settle_poll_s)
        raise TimeoutError(fail_msg)

    def _sleep_abortable(self, seconds: float) -> None:
        deadline = time.perf_counter() + seconds
        while time.perf_counter() < deadline:
            if self._abort.is_set():
                raise SweepAborted()
            time.sleep(0.05)

    def _set_point_state(self, point: SweepPoint, state: str) -> None:
        point.status = state
        if self.cb.on_point_state:
            self.cb.on_point_state(point.row_index, state)

    def _event(self, msg: str) -> None:
        log.info(msg)
        if self.cb.on_event:
            self.cb.on_event(msg)
