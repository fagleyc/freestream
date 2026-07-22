"""TraverseDrive — host-side controller for the SSWT 3-axis traverse.

One control thread polls the WAGO (one block read per tick: positions +
StatusWord) and composes ONE ControlWord write per tick from the
per-axis states, so multi-axis moves are naturally synchronous.

Safety model
------------
* Soft travel limits (calibrated axes) enforced before and during moves.
* **Limit switches (host-side reaction, 2026-07; polarity rig-verified
  2026-07-22 — the StatusWord bit is CLEAR when a switch is engaged,
  SET when healthy; X's limit input is disabled entirely):** the rig's
  limit
  switches work again and land on StatusWord %MW1 bits 0/1/2 (X/Y/Z,
  negative-direction switches). The module hardware-limit lockout is
  UNLINKED (Ptr_LimitSwitch = 0), so the drive never stops itself — if
  an axis's bit trips while it is commanded TOWARD the limit (outside a
  homing sequence) the host stops the axis within one control tick and
  flags a ``LIMIT`` fault. Commanding away from a made switch is
  allowed (that is the recovery path), and the fault clears when the
  bit does. The host is the only protection.
* `stop_all()` / E-stop writes ControlWord = 0 immediately from the
  calling thread (not queued behind the loop).
* Wrong-way trip: if a move's error GROWS for `wrongway_ticks`
  consecutive ticks the axis is stopped — protects the first live runs
  against a wrong `fwd_increases_counts` direction sense.
* Stall detection: an axis commanded with FROZEN counts warns and then
  aborts (the 750-673 counter is open-loop step count, so frozen counts
  mean the module isn't stepping — a faulted/dead module).
* Modbus watchdog: `max_consecutive_errors` failures → stop everything,
  drop the connection, report via `on_status`.

Homing (host-side, per axis)
----------------------------
``home_axis`` jogs the axis toward its NEGATIVE limit at the PLC's
fixed speed, watching the StatusWord bit at the control-loop rate (the
loop tightens to ~15 ms ≈ 66 Hz while a homing cycle runs; the reaction
bound is therefore one tightened tick, ~2000 steps/s × 15 ms ≈ 30
counts of overtravel). When the bit sets the jog is dropped, the axis
backs off the opposite way until the bit clears plus
``home_backoff_margin_s`` (the PLC speed is fixed — no host "slow" jog
— so the margin time bounds the overshoot), stops, and the current
unwrapped counts are calibrated to ``home_datum_in`` via
``calibrate_offset``. Homing is per-power-cycle: a module power cycle
zeroes the counter, so re-home each setup (the offset persists only if
the config is saved). X has no homing (``home_enabled`` False).

The PLC runs the steppers at a fixed ±2000 steps/s (no host speed
control), so a "move to" is a bang-bang run at that speed stopped inside
the tolerance band. Lifecycle/callback shape matches the other AeroVIS
device drivers.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

import numpy as np

from .config import AxisConfig, TraverseConfig
from .datamodel import ScanRingBuffer
from .emulator import SimPlc
from .plc import STATUS_LIMIT_MASK, PlcError, WagoTraversePlc

log = logging.getLogger(__name__)

FIELDS = ["t",
          "X", "Y", "Z",                      # inches (per calibration)
          "X_cnt", "Y_cnt", "Z_cnt"]          # raw DINT counts

# host-side homing phase names (published as state()["home_state"])
HOME_SEEK = "SEEK"
HOME_BACKOFF = "BACKOFF"
HOME_MARGIN = "MARGIN"
HOME_SETTLE = "SETTLE"

# the loop tightens to this period while a homing cycle runs, so the
# limit bit is polled at ~66 Hz instead of the normal 50 ms / 20 Hz
_HOMING_POLL_S = 0.015


@dataclass
class HomingResult:
    """Outcome of one homing cycle (or its acceptance, for wait=False)."""
    ok: bool
    state: str                 # DONE / SEEK_TIMEOUT / ABORTED / …
    fault: str                 # failure reason ("" when ok)
    duration_s: float


class _AxisState:
    def __init__(self, cfg: AxisConfig):
        self.cfg = cfg
        self.counts = 0            # UNWRAPPED (continuous) position
        self.raw_prev: Optional[int] = None   # last raw ring value
        self.inches = 0.0
        # DESIRED direction, in counts space: True=+counts,
        # False=−counts, None = not commanding this axis
        self.command: Optional[bool] = None
        # what is actually in the ControlWord right now — command
        # transitions pass through the direction-change dwell so the
        # 750-673 never sees a start/reversal during its own stop
        # sequence (the likely start/stop fault source)
        self.applied: Optional[bool] = None
        self.dwell_ticks = 0
        # move oscillation guard
        self.last_move_dir: Optional[bool] = None
        self.reversals = 0
        # 750-673 status bytes (S1, S2, S3) from the input image
        self.module_status: tuple = (0, 0, 0)
        self.moving = False                    # move_to in progress
        self.target_counts: Optional[int] = None
        self.target_in: Optional[float] = None
        self.prev_abs_err: Optional[int] = None
        self.wrongway = 0
        # stall detection: commanded but counts frozen (module not
        # stepping — the 750-673 counter is open-loop step count)
        self.stall_last: Optional[int] = None
        self.stall_ticks = 0
        self.stall_warned = False
        # ── limit switch / fault ──
        self.limit = False                     # engaged (decoded polarity)
        self.limit_prev = False                # last tick's engaged state
        self.fault: Optional[str] = None       # e.g. "LIMIT" (state dict)
        # ── host-side homing ──
        self.homing = False                    # cycle in progress
        self.home_phase = ""                   # SEEK/BACKOFF/MARGIN/SETTLE
        self.homed = False                     # last host homing succeeded
        self.home_t0 = 0.0
        self.home_deadline = 0.0               # current phase deadline
        self.home_evt = threading.Event()      # set on finish/teardown
        self.home_result: Optional["HomingResult"] = None


class TraverseDrive:
    """3-axis WAGO traverse (bang-bang positioning, absolute tracking)."""

    def __init__(self, config: Optional[TraverseConfig] = None):
        self.config = config or TraverseConfig()

        self.on_status: Optional[Callable[[str], None]] = None
        self.on_move_complete: Optional[Callable[[str], None]] = None

        self.ring = ScanRingBuffer(FIELDS, capacity=200_000)
        # (t, axis, old_s1, new_s1, counts) — stepper module S1 changes
        self.module_events: deque = deque(maxlen=500)
        self.on_module_status: Optional[Callable[[tuple], None]] = None

        self._st: Dict[str, _AxisState] = {
            c.name: _AxisState(c) for c in self.config.axes()}

        # ControlWord echo from the last poll, for the Diagnostics tab
        self.control_echo = 0
        # StatusWord from the last poll (limit bits 0/1/2)
        self.status_echo = 0

        self._plc = None                       # WagoTraversePlc | SimPlc
        self._connected = False
        self._sim = False
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

    def state(self) -> Dict[str, dict]:
        out = {}
        for s in self._st.values():
            out[s.cfg.name] = {
                "inches": s.inches, "counts": s.counts,
                "moving": s.moving,
                "target": s.target_in,
                "calibrated": s.cfg.calibrated,
                "enabled": s.cfg.enabled,
                "module_status": s.module_status,
                "limit": s.limit,
                "fault": s.fault,
                "homing": s.homing,
                "homed": s.homed,
                "home_state": s.home_phase,
            }
        return out

    def is_homed(self, axis: str) -> bool:
        """True after the last host-side homing cycle of ``axis``
        completed successfully (cleared when a new cycle starts/faults).
        Homing is per-power-cycle — a module power cycle zeroes the
        counter and requires re-homing."""
        return self._st[axis.upper()].homed

    # ── configuration ────────────────────────────────────────────────────
    def set_config(self, config: TraverseConfig) -> None:
        """Adopt a new configuration (e.g. after loading a JSON file).

        Rebinds the axis states to the new AxisConfig instances so
        calibration/limits/tolerances take effect immediately (same
        regression the crescent hit: without the rebind a loaded config
        is silently ignored by the running drive). IP/port still only
        apply at the next connect.
        """
        with self._cmd_lock:
            self.config = config
            for name, ax_cfg in (("X", config.x), ("Y", config.y),
                                 ("Z", config.z)):
                st = self._st[name]
                st.cfg = ax_cfg
                st.inches = ax_cfg.counts_to_inches(st.counts)

    # ── host-side homing ─────────────────────────────────────────────────
    @staticmethod
    def _neg_inches_dir(cfg: AxisConfig) -> bool:
        """Counts direction (True = +counts) that DECREASES inches.

        inches = inch_high − (counts_high − counts)/clicks_per_inch, so
        d(inches)/d(counts) = 1/clicks_per_inch: a negative slope means
        +counts moves toward −inches. The ControlWord bit actually
        written comes from _mask_for (fwd_increases_counts sense).
        VERIFY direction live on the first supervised homing run.
        """
        return cfg.clicks_per_inch < 0

    @staticmethod
    def _seek_dir(cfg: AxisConfig) -> bool:
        """Homing SEEK counts-direction (True = +counts) for this axis.

        The homing direction is pinned at the BIT level
        (``home_jog_fwd``: True = seek jogs fwd_mask, False = rev_mask)
        — deliberately independent of the position-mode bookkeeping,
        because the rig (2026-07-22) needs position mode and homing on
        OPPOSITE senses. This helper converts the configured bit back
        into the counts-direction the state machine carries, so
        ``_mask_for`` at write time emits exactly the configured bit.
        The limit-switch side IS the seek side: runtime recovery is
        always the opposite (:meth:`_away_dir`).
        """
        return (cfg.home_jog_fwd if cfg.fwd_increases_counts
                else not cfg.home_jog_fwd)

    @classmethod
    def _away_dir(cls, cfg: AxisConfig) -> bool:
        """Counts-direction AWAY from the axis's limit switch (recovery)."""
        return not cls._seek_dir(cfg)

    def _limit_engaged(self, status: int, cfg: AxisConfig) -> bool:
        """True when this axis's limit switch is ENGAGED.

        Rig-verified 2026-07-22: the StatusWord bit is CLEAR when the
        switch is pressed and SET when healthy (NC chain drives the
        input high) — ``limit_active_low=True`` implements that
        reversed sense. Axes with ``limit_enabled=False`` (X/Axial per
        the rig) always report not-engaged: their input is ignored for
        both homing and the runtime limit reaction.
        """
        if not cfg.limit_enabled:
            return False
        bit = bool(status & STATUS_LIMIT_MASK[cfg.name])
        return (not bit) if self.config.limit_active_low else bit

    def home_axis(self, axis: str, wait: bool = True,
                  timeout_s: Optional[float] = None) -> HomingResult:
        """Home one axis to its NEGATIVE limit switch (host-side).

        Sequence (driven by the control loop, limit bit polled every
        tick — the loop tightens to ~15 ms while homing): jog toward
        −inches until the axis's StatusWord limit bit sets → stop → jog
        the opposite way until the bit clears plus
        ``home_backoff_margin_s`` → stop → ``calibrate_offset(
        home_datum_in, counts)`` so the limit position reads the datum
        → homed. Phase deadlines (``home_seek_timeout_s`` /
        ``home_backoff_timeout_s``) fault cleanly: axis stopped, homed
        stays False. Per-axis stop, ``stop_all`` and E-STOP abort the
        cycle and leave the axis stopped.

        ``wait=False`` returns right after the seek starts;
        ``wait=True`` blocks the CALLER thread (never the control
        thread) until the cycle finishes, up to ``timeout_s`` (default:
        the phase timeouts + margin + slack).

        Homing is PER-POWER-CYCLE: the 750-673 counter zeroes at module
        power-up, so re-home each setup (the offset persists only if
        the config is saved). Raises ValueError for axes with
        ``home_enabled`` off (X — no homing on the axial axis).
        """
        st = self._st[axis.upper()]
        cfg = st.cfg
        if not cfg.home_enabled:
            raise ValueError(
                f"no homing on {cfg.name} — the {cfg.label} axis has no "
                f"homing sequence (home_enabled is off)")
        if not cfg.limit_enabled:
            raise ValueError(
                f"cannot home {cfg.name} — its limit switch input is "
                f"disabled (limit_enabled is off)")
        if not cfg.enabled:
            raise ValueError(f"{cfg.name} axis is disabled")
        if not self._connected or self._plc is None:
            raise RuntimeError("connect() first")
        with self._cmd_lock:
            if any(s.moving or s.command is not None
                   for s in self._st.values()):
                raise RuntimeError(
                    "axes are moving — stop all motion before homing")
            if any(s.homing for s in self._st.values()):
                raise RuntimeError("a homing cycle is already running")
            # reserve the axis NOW (atomically with the checks) so no
            # move_to/home_axis can slip in
            st.homing = True
            st.home_phase = HOME_SEEK
            st.homed = False
            st.fault = None
            st.home_t0 = time.perf_counter()
            st.home_deadline = (st.home_t0 +
                                self.config.home_seek_timeout_s)
            st.home_evt.clear()
            st.home_result = None
        seek_bit = "FWD" if cfg.home_jog_fwd else "REV"
        self._status(f"{cfg.name} homing: SEEK ({seek_bit} bit) toward "
                     f"the limit (datum {cfg.home_datum_in:+.2f}\")")
        if not wait:
            return HomingResult(ok=True, state=HOME_SEEK, fault="",
                                duration_s=0.0)
        # blocking wait in the CALLER thread; the control loop runs the
        # sequence and sets the event
        total = (float(timeout_s) if timeout_s is not None else
                 self.config.home_seek_timeout_s +
                 self.config.home_backoff_timeout_s +
                 self.config.home_backoff_margin_s + 10.0)
        if st.home_evt.wait(total) and st.home_result is not None:
            return st.home_result
        # waiter timeout: stop the axis (aborts the cycle) and close out
        self.stop_axis(cfg.name)
        st.home_evt.wait(2.0)
        with self._cmd_lock:
            if st.home_result is None:          # loop never closed it
                st.homing = False
                st.home_phase = ""
                st.home_result = HomingResult(
                    ok=False, state="TIMEOUT", fault="TIMEOUT",
                    duration_s=time.perf_counter() - st.home_t0)
                st.home_evt.set()
        return st.home_result

    def abort_homing(self, axis: str) -> None:
        """Abort a running homing cycle: stop the axis, homed stays
        False (no-op when the axis isn't homing)."""
        st = self._st[axis.upper()]
        with self._cmd_lock:
            if not st.homing:
                return
            self._finish_homing(st, ok=False, state="ABORTED",
                                fault="ABORTED")
            st.command = None
            st.applied = None
        self._write_now(f"{st.cfg.name} homing ABORTED — axis stopped")

    def _tick_homing(self, st: _AxisState) -> None:
        """Advance a homing axis one control tick (loop, lock held).

        The limit bit (st.limit) was refreshed from this tick's
        StatusWord read just before this call.
        """
        if not st.homing:
            return
        cfg = st.cfg
        now = time.perf_counter()
        seek_dir = self._seek_dir(cfg)

        if st.home_phase == HOME_SEEK:
            if st.limit:
                # switch made: drop the jog (this tick's ControlWord
                # write), then back off the other way
                st.command = None
                st.home_phase = HOME_BACKOFF
                st.home_deadline = (now +
                                    self.config.home_backoff_timeout_s)
                self._status(f"{cfg.name} homing: limit switch made "
                             f"@ {st.counts:+d} counts — backing off")
            elif now > st.home_deadline:
                st.command = None
                self._finish_homing(st, ok=False, state="SEEK_TIMEOUT",
                                    fault="SEEK_TIMEOUT")
            else:
                st.command = seek_dir
        elif st.home_phase == HOME_BACKOFF:
            if not st.limit:
                # switch released: keep jogging away for the margin
                # (fixed PLC speed — the margin time bounds overshoot)
                st.home_phase = HOME_MARGIN
                st.home_deadline = (now +
                                    self.config.home_backoff_margin_s)
                st.command = not seek_dir
            elif now > st.home_deadline:
                st.command = None
                self._finish_homing(st, ok=False, state="BACKOFF_TIMEOUT",
                                    fault="BACKOFF_TIMEOUT")
            else:
                st.command = not seek_dir
        elif st.home_phase == HOME_MARGIN:
            if now >= st.home_deadline:
                st.command = None
                st.home_phase = HOME_SETTLE
                # let the PLC's ~250 ms stop sequence finish before the
                # datum counts are captured
                st.home_deadline = now + max(
                    self.config.direction_dwell_ms / 1000.0, 0.25)
            else:
                st.command = not seek_dir
        elif st.home_phase == HOME_SETTLE:
            st.command = None
            if now >= st.home_deadline:
                # the datum moment: current unwrapped counts read the
                # configured datum (the limit itself reads the datum to
                # within the backoff-margin travel)
                cfg.calibrate_offset(cfg.home_datum_in, st.counts)
                st.inches = cfg.counts_to_inches(st.counts)
                self._finish_homing(st, ok=True, state="DONE", fault="")

    def _finish_homing(self, st: _AxisState, ok: bool, state: str,
                       fault: str) -> None:
        """Close out a homing cycle (loop or teardown; lock held)."""
        st.homing = False
        st.home_phase = ""
        st.homed = ok
        st.command = None
        res = HomingResult(ok=ok, state=state, fault=fault,
                           duration_s=time.perf_counter() - st.home_t0)
        st.home_result = res
        st.home_evt.set()
        if ok:
            self._status(f"{st.cfg.name} HOMED in {res.duration_s:.1f} s "
                         f"— datum {st.cfg.home_datum_in:+.2f}\" "
                         f"(now {st.inches:+.3f}\", {st.counts:+d} counts)")
        else:
            self._status(f"{st.cfg.name} homing FAILED ({state}) — "
                         f"axis stopped, not homed")

    # ── lifecycle ────────────────────────────────────────────────────────
    def connect(self) -> None:
        if self._connected:
            return
        self._sim = self.config.force_sim
        if self._sim:
            self._plc = SimPlc(self.config)
        else:
            self._plc = WagoTraversePlc(self.config.ip, self.config.port,
                                        self.config.unit_id,
                                        self.config.modbus_timeout_s)
        self._plc.connect()
        if hasattr(self._plc, "module_status_supported"):
            self._plc.module_status_supported = \
                self.config.read_module_status
        # known-safe starting state: everything stopped
        self._plc.write_control(0, force=True)

        reading = self._plc.read_block()
        self.control_echo = reading.control
        self.status_echo = reading.status
        with self._cmd_lock:
            for st in self._st.values():
                st.command = None
                st.applied = None
                st.dwell_ticks = 0
                if reading.module_status:
                    st.module_status = reading.module_status.get(
                        st.cfg.name, (0, 0, 0))     # baseline, no event
                st.moving = False
                st.target_counts = None
                st.target_in = None
                st.homing = False
                st.home_phase = ""
                st.fault = None
                st.limit = self._limit_engaged(reading.status, st.cfg)
                # (st.homed is KEPT across reconnects, like raw_prev —
                # the module counter survives a host reconnect; only a
                # module POWER cycle invalidates homing)
                # raw_prev is deliberately KEPT across reconnects: the
                # unwrap (and with it the offset calibration) stays
                # valid as long as the axis moved less than half the
                # 1M-count ring while we weren't watching
                self._apply_counts(st, reading.counts[st.cfg.name])
                st.inches = st.cfg.counts_to_inches(st.counts)

        self._errors = 0
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="traverse-loop", daemon=True)
        self._thread.start()
        self._connected = True
        mode = "SIM" if self._sim else "LIVE"
        pos = ", ".join(f"{s.cfg.name} {s.counts:+d}"
                        for s in self._st.values() if s.cfg.enabled)
        self._status(f"Connected ({mode}) — {pos} counts")
        if not all(s.cfg.calibrated for s in self._st.values()
                   if s.cfg.enabled) and not self._sim:
            self._status("WARNING: axis calibration not entered — "
                         "positions in raw counts (home Y/Z to "
                         "calibrate their offsets)")

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
        if self._plc is not None:
            try:                    # leave the PLC commanding nothing
                self._plc.write_control(0, force=True)
            except Exception:                          # noqa: BLE001
                pass
            self._plc.close()
            self._plc = None
        self._connected = False
        # release any home_axis(wait=True) caller still blocked
        with self._cmd_lock:
            for st in self._st.values():
                if st.homing:
                    self._finish_homing(st, ok=False,
                                        state="DISCONNECTED",
                                        fault="DISCONNECTED")
        self._status("Disconnected")

    # ── direction helpers ────────────────────────────────────────────────
    @staticmethod
    def _mask_for(cfg: AxisConfig, counts_up: bool) -> int:
        fwd = counts_up if cfg.fwd_increases_counts else not counts_up
        return cfg.fwd_mask if fwd else cfg.rev_mask

    @staticmethod
    def _apply_counts(st: _AxisState, raw: int,
                      max_jump: Optional[int] = None) -> bool:
        """Fold the module's wrapping counter into an ABSOLUTE position.

        The 750-673 counter is configured to roll over cleanly at
        ``wrap_modulus`` counts (unsigned 0…m−1, default 1,000,000 —
        999999→0 going up, 0→999999 going down). Each tick moves ≪ half
        the ring, so the shortest-path modular delta is the true
        motion; accumulating it gives a continuous absolute position
        (``st.counts``, unbounded, may exceed the modulus in either
        direction) that survives any number of wrap crossings.

        ``max_jump``: a per-tick delta larger than any physically
        possible motion means the COUNTER changed, not the carriage
        (module reset / power event). The position is then HELD and the
        raw baseline re-based instead of integrating a phantom move —
        returns True so the caller can warn. The calibration stays
        valid because the held position is the truth.
        """
        m = st.cfg.wrap_modulus
        if not m:
            st.counts = raw
            return False
        raw %= m                      # normalize to the unsigned ring
        if st.raw_prev is None:
            st.counts = raw
            st.raw_prev = raw
            return False
        delta = (raw - st.raw_prev + m // 2) % m - m // 2
        st.raw_prev = raw
        if max_jump is not None and abs(delta) > max_jump:
            return True
        st.counts += delta
        return False

    # ── motion commands ──────────────────────────────────────────────────
    def move_to(self, x: Optional[float] = None, y: Optional[float] = None,
                z: Optional[float] = None) -> None:
        """Start a move (inches); all commanded axes begin the same tick.

        Raises on uncalibrated axes or soft-limit violations — before any
        motion.
        """
        if not self._connected:
            raise RuntimeError("connect() first")
        if any(s.homing for s in self._st.values()):
            raise RuntimeError("homing in progress — wait for it to "
                               "finish (or abort_homing) before moving")
        wanted = [(v, self._st[n]) for v, n in
                  ((x, "X"), (y, "Y"), (z, "Z")) if v is not None]
        for value, st in wanted:
            cfg = st.cfg
            if not cfg.enabled:
                raise ValueError(f"{cfg.name} axis is disabled")
            if not cfg.calibrated:
                raise ValueError(f"{cfg.name} is not calibrated — "
                                 f"position moves disabled")
            if not cfg.min_in <= value <= cfg.max_in:
                raise ValueError(
                    f"{cfg.name} target {value:+.3f}\" outside limits "
                    f"[{cfg.min_in:+.2f}, {cfg.max_in:+.2f}]")
        with self._cmd_lock:
            started = []
            for value, st in wanted:
                st.fault = None            # new command: clear the flag
                st.target_in = float(value)
                st.target_counts = st.cfg.inches_to_counts(value)
                st.prev_abs_err = None
                st.wrongway = 0
                st.reversals = 0
                st.last_move_dir = None
                tol = abs(st.cfg.tolerance_in * st.cfg.clicks_per_inch)
                st.moving = abs(st.counts - st.target_counts) > tol
                if st.moving:
                    started.append(f"{st.cfg.name}→{value:+.3f}\"")
        if started:
            self._status("Moving " + " + ".join(started))

    def stop_axis(self, name: str) -> None:
        st = self._st[name.upper()]
        aborted = False
        with self._cmd_lock:
            st.moving = False
            st.target_counts = None
            st.target_in = None
            st.command = None
            st.applied = None
            if st.homing:              # per-axis stop aborts homing too
                self._finish_homing(st, ok=False, state="ABORTED",
                                    fault="ABORTED")
                aborted = True
        self._write_now(f"{st.cfg.name} stopped" +
                        (" (homing aborted)" if aborted else ""))

    def stop_all(self) -> None:
        """Immediate stop of all axes (E-stop path, synchronous write)."""
        with self._cmd_lock:
            for st in self._st.values():
                st.moving = False
                st.target_counts = None
                st.target_in = None
                st.command = None
                st.applied = None
                if st.homing:          # E-stop aborts any homing cycle
                    self._finish_homing(st, ok=False, state="ABORTED",
                                        fault="ABORTED")
            if self._plc is not None and self._plc.connected:
                try:
                    self._plc.write_control(0, force=True)
                except PlcError as exc:
                    self._status(f"STOP write failed: {exc}")
        self._status("STOP issued (all axes)")

    # ── control loop ─────────────────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            try:
                if not self._loop_tick():
                    break
            except Exception as exc:                   # noqa: BLE001
                # NOTHING may kill this thread with axes commanded — a
                # raw pymodbus timeout did exactly that live 2026-07-07
                log.exception("control loop error")
                self._status(f"CONTROL LOOP ERROR: {exc} — emergency "
                             f"stop + disconnect")
                with self._cmd_lock:
                    self._watchdog_trip()
                break

    def _loop_tick(self) -> bool:
        """One control tick; False = loop should exit."""
        t0 = time.perf_counter()
        period = self.config.loop_ms / 1000.0
        completed: List[str] = []
        with self._cmd_lock:
            if self._plc is None:
                return False
            try:
                reading = self._plc.read_block()
                self._errors = 0
            except PlcError as exc:
                self._errors += 1
                self._status(f"PLC read error ({self._errors}): {exc}")
                if self._errors >= self.config.max_consecutive_errors:
                    self._watchdog_trip()
                    return False
                self._stop_evt.wait(period)
                return True

            self.control_echo = reading.control
            self.status_echo = reading.status

            for st in self._st.values():
                if not st.cfg.enabled:
                    st.command = None
                    st.applied = None
                    continue
                jumped = self._apply_counts(
                    st, reading.counts[st.cfg.name],
                    self.config.max_counts_per_tick)
                if jumped:
                    self._status(
                        f"WARNING: {st.cfg.name} position counter "
                        f"JUMPED (module reset/power event?) — "
                        f"position held at {st.counts:+d}; verify "
                        f"against a known reference (re-home)")
                st.inches = st.cfg.counts_to_inches(st.counts)
                st.limit = self._limit_engaged(reading.status, st.cfg)
                self._tick_motion(st, completed)
                self._tick_homing(st)
                self._tick_limit(st)
                self._tick_stall(st)
                self._tick_module(st, reading.module_status)
                self._tick_apply(st)

            word = self._compose_word()
            try:
                self._plc.write_control(word)      # change-only
            except PlcError as exc:
                self._errors += 1
                self._status(f"PLC write error ({self._errors}): {exc}")
                if self._errors >= self.config.max_consecutive_errors:
                    self._watchdog_trip()
                    return False

            # tighter limit-bit poll while a homing cycle runs
            if any(s.homing for s in self._st.values()):
                period = min(period, _HOMING_POLL_S)

        self.ring.push_block({
            "t": np.array([time.time()]),
            **{n: np.array([self._st[n].inches]) for n in "XYZ"},
            **{f"{n}_cnt": np.array([float(self._st[n].counts)])
               for n in "XYZ"},
        })
        for name in completed:
            self._status(f"{name} move complete")
            if self.on_move_complete:
                self.on_move_complete(name)

        elapsed = time.perf_counter() - t0
        self._stop_evt.wait(max(period - elapsed, 0.005))
        return True

    def _tick_limit(self, st: _AxisState) -> None:
        """Host-side limit reaction (loop, lock held; after motion/homing
        ticks so st.command reflects this tick's desired direction).

        The module hardware lockout is UNLINKED (Ptr_LimitSwitch = 0) —
        the module will happily keep stepping into the switch, so the
        HOST must drop the jog. An axis commanded TOWARD the negative
        limit while its bit is set (outside homing — homing owns the
        bit) is stopped this tick and flagged with a ``LIMIT`` fault.
        Commanding AWAY from a made switch is allowed (the recovery
        path); the fault clears when the bit does.
        """
        if st.homing:
            return
        if not st.limit:
            if st.fault == "LIMIT":
                st.fault = None        # backed off the switch: clear
            st.limit_prev = False
            return
        # The switch is ENGAGED. Rig-found 2026-07-22: a position move
        # drove INTO the limit without stopping because the old reaction
        # only fired when the DIRECTION BOOKKEEPING said "heading
        # negative" — a sign-convention error defeated the protection.
        # Two rules now, so bookkeeping can never defeat it again:
        #  1. the ENGAGE TRANSITION while anything is commanded stops
        #     the axis UNCONDITIONALLY (whatever the claimed direction —
        #     the plant just proved it was driving into the switch);
        #  2. while engaged, only a FRESH command in the away direction
        #     (+inches, off the negative switch) is allowed to run —
        #     the recovery path (move_to/jog away after the stop).
        newly_engaged = not st.limit_prev
        st.limit_prev = True
        commanded = st.command is not None or st.applied is not None
        if not commanded:
            return
        away = self._away_dir(st.cfg)
        heading_away = (st.command == away and
                        st.applied in (None, away))
        if not newly_engaged and heading_away:
            return                     # deliberate recovery — let it run
        st.moving = False
        st.target_counts = None
        st.target_in = None
        st.command = None
        st.applied = None              # bit drops in THIS tick's write
        if st.fault != "LIMIT":
            st.fault = "LIMIT"
            away_sign = ("−" if self._neg_inches_dir(st.cfg) == away
                         else "+")
            self._status(
                f"LIMIT: {st.cfg.name} limit switch ENGAGED — axis "
                f"stopped (host-side reaction; the module no longer "
                f"stops itself; motion is cut regardless of the "
                f"commanded direction). Command {away_sign}inches "
                f"(away) to recover")

    def _tick_motion(self, st: _AxisState, completed: List[str]) -> None:
        cfg = st.cfg
        if st.homing:
            return                     # homing owns st.command
        if not st.moving or st.target_counts is None:
            st.command = None
            return

        # target error is ABSOLUTE (unwrapped) counts vs the absolute
        # accumulated position — never the raw ring value — so targets
        # any number of wrap crossings away converge
        err = st.target_counts - st.counts
        tol = abs(cfg.tolerance_in * cfg.clicks_per_inch)
        if abs(err) <= tol:
            st.moving = False
            st.target_counts = None
            st.command = None
            completed.append(cfg.name)
            return

        # wrong-way trip: error must not keep growing while commanding
        if st.prev_abs_err is not None and st.command is not None:
            if abs(err) > st.prev_abs_err + max(tol * 0.1, 1):
                st.wrongway += 1
                if st.wrongway >= self.config.wrongway_ticks:
                    st.moving = False
                    st.target_counts = None
                    st.command = None
                    self._status(
                        f"WRONG WAY: {cfg.name} moved AWAY from the "
                        f"target — stopped. Check the "
                        f"'{cfg.name} forward increases counts' setting")
                    return
            else:
                st.wrongway = 0
        st.prev_abs_err = abs(err)

        desired = err > 0
        if st.last_move_dir is not None and desired != st.last_move_dir:
            st.reversals += 1
            if st.reversals > self.config.max_reversals:
                st.moving = False
                st.target_counts = None
                st.command = None
                self._status(
                    f"ABORTED: {cfg.name} move oscillating around the "
                    f"target ({st.reversals} reversals) — tolerance "
                    f"{cfg.tolerance_in:g}\" is too tight for the "
                    f"PLC's fixed speed; stopped {st.inches:+.4f}\"")
                return
        st.last_move_dir = desired
        st.command = desired

    def _tick_apply(self, st: _AxisState) -> None:
        """Command shaping: transitions pass through the stop dwell.

        Stops apply IMMEDIATELY. A start (None→dir) or a reversal
        (dir→dir') first commands a stop, then waits
        ``direction_dwell_ms`` — longer than the PLC's own 250 ms
        stop/disable sequence — before the new direction goes out, so
        the module is never hit with a conflicting command
        mid-sequence.
        """
        if st.dwell_ticks > 0:
            st.dwell_ticks -= 1
        if st.command == st.applied:
            return
        if st.applied is not None:
            # leaving an active direction (stop or reversal): stop NOW
            st.applied = None
            st.dwell_ticks = max(1, round(self.config.direction_dwell_ms /
                                          max(self.config.loop_ms, 1)))
        elif st.command is not None and st.dwell_ticks == 0:
            st.applied = st.command

    def _tick_module(self, st: _AxisState, module_status) -> None:
        """Track the 750-673 status bytes; log every S1 transition.

        The exact bit meanings aren't documented in the extracted
        source (the MC3 lib derives BasicError/BasicBusy from them), so
        the Diagnostics log records each change with position and
        command context — one faulting start on the rig identifies the
        error bit empirically.
        """
        if not module_status:
            return
        ms = module_status.get(st.cfg.name)
        if ms is None or ms == st.module_status:
            return
        old_s1 = st.module_status[0]
        if ms[0] != old_s1:
            ev = (time.time(), st.cfg.name, old_s1, ms[0], st.counts)
            self.module_events.append(ev)
            self._status(f"{st.cfg.name} module S1 0x{old_s1:02X} → "
                         f"0x{ms[0]:02X} @ {st.counts:+d}")
            if self.on_module_status:
                self.on_module_status(ev)
        st.module_status = ms

    def _tick_stall(self, st: _AxisState) -> None:
        """Warn when an axis is commanded but its counts are frozen.

        The 750-673 position counter is the module's own step count
        (open loop), so frozen counts mean the module is NOT issuing
        steps at all — drive fault/disabled or the PLC isn't acting on
        the bit. Counts that move while the carriage doesn't point at
        motor power/wiring instead.
        """
        # only count ticks where the command is actually APPLIED (a
        # dwell hold is not a stall)
        if st.command is None or st.applied is None:
            st.stall_last = None
            st.stall_ticks = 0
            st.stall_warned = False
            return
        if st.stall_last is not None and st.counts == st.stall_last:
            st.stall_ticks += 1
            if (st.stall_ticks >= self.config.stall_ticks
                    and not st.stall_warned):
                st.stall_warned = True
                self._status(
                    f"STALL: {st.cfg.name} commanded but counts are "
                    f"frozen — the stepper module is not stepping "
                    f"(faulted/disabled?). If counts move but the "
                    f"carriage doesn't, check motor power/wiring")
            if st.stall_ticks >= self.config.stall_abort_ticks:
                st.moving = False
                st.target_counts = None
                st.target_in = None
                st.command = None
                st.stall_ticks = 0
                st.stall_warned = False
                if st.homing:          # a stalled homing cycle faults
                    self._finish_homing(st, ok=False, state="STALLED",
                                        fault="STALLED")
                self._status(
                    f"ABORTED: {st.cfg.name} move — module not "
                    f"stepping (S1 0x{st.module_status[0]:02X}). "
                    f"Check the module and retry")
        else:
            st.stall_ticks = 0
            st.stall_warned = False
        st.stall_last = st.counts

    def _watchdog_trip(self) -> None:
        self._status("WATCHDOG: PLC unreachable — stopping all axes and "
                     "disconnecting")
        for st in self._st.values():
            st.moving = False
            st.target_counts = None
            st.target_in = None
            st.command = None
            if st.homing:              # unblock any home_axis waiter
                self._finish_homing(st, ok=False,
                                    state="DISCONNECTED",
                                    fault="DISCONNECTED")
        if self._plc is not None:
            try:
                self._plc.write_control(0, force=True)
            except Exception:                          # noqa: BLE001
                pass
            self._plc.close()
            self._plc = None
        self._connected = False

    # ── helpers ──────────────────────────────────────────────────────────
    def _compose_word(self) -> int:
        """Assemble the ControlWord from every axis's applied command.

        Homing axes get their bit DIRECTLY from ``home_jog_fwd`` (seek
        jogs that bit, backoff the opposite) — never remapped through
        the position-mode ``fwd_increases_counts`` bookkeeping. The rig
        (2026-07-22) needs position mode and homing on OPPOSITE senses,
        and no configuration state may re-couple them.
        """
        word = 0
        for st in self._st.values():
            if st.applied is None:
                continue
            if st.homing:
                fwd = (st.cfg.home_jog_fwd
                       if st.home_phase == HOME_SEEK
                       else not st.cfg.home_jog_fwd)
                word |= st.cfg.fwd_mask if fwd else st.cfg.rev_mask
            else:
                word |= self._mask_for(st.cfg, st.applied)
        return word

    def _write_now(self, msg: str) -> None:
        """Compose + force-write the ControlWord from the calling thread."""
        with self._cmd_lock:
            word = self._compose_word()
            if self._plc is not None and self._plc.connected:
                try:
                    self._plc.write_control(word, force=True)
                except PlcError as exc:
                    self._status(f"stop write failed: {exc}")
        self._status(msg)

    def _status(self, msg: str) -> None:
        log.info(msg)
        if self.on_status:
            self.on_status(msg)
