"""Sweep engine — the per-point state machine (spec §6).

Plain Python, no Qt: the GUI runs :meth:`SweepEngine.run` on a worker
thread and receives progress via callbacks; tests drive it directly in
sim. Per-point cycle::

    [refuse-to-record check] → set tunnel Mach (MachLoop → RPM command;
    meta["rpm"] = direct-RPM override bypass) → wait at_target →
    move positioner(alpha, beta) → wait settled → (optional zero) →
    drain stale → dwell → acquire → write .h5 → advance

MONITOR-ONLY (``config.tunnel_control_enabled = False``, the default —
the Red Lion rejects Block2 writes until the Crimson fix): the "set
tunnel" stage NEVER touches the SetpointDevice. Instead the engine
raises an :class:`OperatorWaitRequest` through
``callbacks.on_operator_wait`` — the OPERATOR brings the console to the
target Mach (or RPM for a run-sheet ``rpm`` override) and the callback
answers "proceed" | "skip" | "abort". Headless (no callback): a clear
event is logged and the point proceeds immediately, still recording the
honest measured Mach/RPM.

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

from .config import FreestreamConfig
from .derived import tunnel_state
from .hal import Positioner, SetpointDevice, Streaming, Zeroable
from .machloop import MachLoop, find_tunnel_daq, make_tunnel_measure
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
    DaqBook stream (NaN when unavailable) + setpoint RPM readback; reads
    only, never a command. ``tolerance`` is the ± band on the target
    quantity (Mach band for mach points, RPM band for rpm overrides)."""
    target_mach: Optional[float]
    tolerance: float
    measure: Callable[[], Tuple[float, float]]
    target_rpm: Optional[float] = None

    @property
    def is_rpm(self) -> bool:
        return self.target_mach is None

    def describe(self) -> str:
        return (f"{self.target_rpm:g} RPM" if self.is_rpm
                else f"Mach {self.target_mach:g}")


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
        self.manager.stop_all_motion()
        self._event("E-STOP: all motion stopped, sweep aborted")

    # ── main entry (call on a worker thread) ─────────────────────────────
    def run(self, points: List[SweepPoint]) -> List[PointOutcome]:
        self._abort.clear()
        self._pause.clear()
        self._running = True
        outcomes: List[PointOutcome] = []
        try:
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
        if self.cb.on_finished:
            self.cb.on_finished(outcomes)
        return outcomes

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
        if not self.config.tunnel_control_enabled:
            if not self.config.mach_check_enabled:
                # Mach verification DISABLED: skip the per-point gate
                # entirely — no operator dialog, no settle wait. Record
                # immediately after positioning; the requested condition
                # still lands in /Tunnel (Mach_cmd/RPM_cmd) alongside the
                # honest measured channels.
                target_rpm = (None if rpm_override is None
                              else float(rpm_override))
                target_mach = (float(point.mach)
                               if rpm_override is None else None)
                self._tunnel_cmd = {"rpm_cmd": target_rpm,
                                    "mach_cmd": target_mach}
                self._event(
                    "tunnel: Mach verification disabled — recording "
                    "immediately without waiting for tunnel conditions")
                return
            # MONITOR-ONLY (Red Lion rejects Block2 writes until the
            # Crimson fix): NEVER touch the SetpointDevice — the OPERATOR
            # sets the console; we wait/prompt, then record honestly.
            self._operator_wait(point, rpm_override, dev)
            return
        if rpm_override is not None:
            # documented direct-RPM override (run-sheet "rpm" column) —
            # commands the fan verbatim, BYPASSING the Mach loop.
            rpm = float(rpm_override)
            self._event(f"tunnel → {rpm:g} RPM (direct override, "
                        f"Mach loop bypassed)")
            dev.set_target(rpm=rpm)
            self._wait(dev.at_target, self.config.tunnel_timeout_s,
                       f"tunnel never reached {rpm:g} RPM")
            self._tunnel_cmd["rpm_cmd"] = rpm
            return
        loop = MachLoop(dev, self.config,
                        daq=find_tunnel_daq(self.manager.streaming),
                        event=self._event)
        result = loop.run(float(point.mach), self._wait)
        self._tunnel_cmd = {"rpm_cmd": result.rpm_cmd,
                            "mach_cmd": result.mach_target}

    def _operator_wait(self, point: SweepPoint, rpm_override,
                       dev: SetpointDevice) -> None:
        """Monitor-only tunnel stage: prompt the operator (or, headless,
        log + proceed) instead of commanding the fan. NO writes here —
        only DAQ/readback reads via the request's measure()."""
        if rpm_override is not None:
            target_mach: Optional[float] = None
            target_rpm: Optional[float] = float(rpm_override)
            tol = max(abs(target_rpm) * 0.01, 1.0)     # ±1 % (≥1 RPM) band
        else:
            target_mach = float(point.mach)
            target_rpm = None
            tol = float(self.config.mach_tolerance)
        req = OperatorWaitRequest(
            target_mach=target_mach, tolerance=tol, target_rpm=target_rpm,
            measure=make_tunnel_measure(
                find_tunnel_daq(self.manager.streaming), dev))
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
                rb = setpoint.readback()
                rpm_cmd = self._tunnel_cmd.get("rpm_cmd")
                tun_samples.setdefault("RPM_meas", []).append(
                    rb.get("rpm", 0.0))
                tun_samples.setdefault("RPM_cmd", []).append(
                    rpm_cmd if rpm_cmd is not None
                    else rb.get("rpm_set", 0.0))
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
            units["Tunnel"] = {k: "RPM" for k in tun_samples}
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
        the live monitors and the MachLoop number-for-number."""
        daq = blocks.get("DaqBook2005", {})
        need = ("Pdiff", "Ptot", "Temp")
        if not all(k in daq and len(daq[k]) for k in need):
            return
        st = tunnel_state(float(np.mean(daq["Pdiff"])),
                          float(np.mean(daq["Ptot"])),
                          float(np.mean(daq["Temp"])))
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
