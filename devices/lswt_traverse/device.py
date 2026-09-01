"""LswtTraverseDrive — 3-axis control for the South LSWT traverse.

Three IDC SmartStep23 SmartDrives on one RS-232C daisy chain. Unlike
the SWT WAGO traverse (host-side bang-bang over direction bits), the
SmartSteps position THEMSELVES: the host sends ``AC/DE/VE/DA…GO`` and
the drive runs its own profile, so this driver is mostly a transactor
plus a monitor thread.

Referencing (the deliberately simple story):

* no homing routine, no limit-switch choreography;
* the operator jogs an axis to its reference spot and calls
  :meth:`set_home` — wire ``SP<datum>`` — which marks the axis
  ``referenced``;
* every absolute move is then gated HOST-side against the axis's soft
  travel limits (``AxisConfig.min_in``/``max_in``), and a jog that
  crosses a limit on a referenced axis is auto-stopped by the monitor.

Absolute moves are REFUSED until an axis is referenced — a SmartStep
wakes up reading 0.000 wherever it happens to stand, so an
unreferenced "go to +5" is a blind lunge. Jogs are always allowed
(that is how you reach the reference spot).

Serial discipline: ONE lock serializes every transaction (write →
consume echo → parse the ``*``-prefixed reply). The chain's mandatory
echo means each read starts with our own bytes; the parser discards
them. All public methods are thread-safe.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Optional

import numpy as np

from . import protocol as P
from .config import AxisConfig, TraverseConfig
from .datamodel import ScanRingBuffer
from .emulator import SimChain
from .protocol import ProtocolError

log = logging.getLogger(__name__)

AXES = ("X", "Y", "Z")


class _AxisState:
    """Live per-axis bookkeeping (driver-internal)."""

    def __init__(self, cfg: AxisConfig):
        self.cfg = cfg
        self.position: float = 0.0
        self.moving = False
        self.enabled = True
        self.fault = ""               # human-readable drive fault, "" = ok
        self.referenced = False       # Set home pressed this session
        self.target: Optional[float] = None
        self.move_started: float = 0.0
        self.jogging = 0              # −1 / 0 / +1: sign of an active jog
        self.state_text = "idle"


class LswtTraverseDrive:
    """Public driver API (mirrors the house TraverseDrive shape)."""

    def __init__(self, config: Optional[TraverseConfig] = None):
        self.config = config or TraverseConfig.load_defaults()
        self.on_status: Optional[Callable[[str], None]] = None
        self.ring = ScanRingBuffer(("t", "X", "Y", "Z"))

        self._serial = None           # pyserial Serial or SimChain
        self._io_lock = threading.RLock()
        self._state: Dict[str, _AxisState] = {}
        self._monitor: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._t0 = time.monotonic()
        self._cycle = 0

    # ── introspection ────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._serial is not None

    @property
    def sim_mode(self) -> bool:
        return isinstance(self._serial, SimChain)

    def state(self) -> Dict[str, dict]:
        """Snapshot per axis: position, moving, referenced, limits…"""
        out = {}
        for name, st in self._state.items():
            out[name] = {
                "position": st.position, "moving": st.moving,
                "enabled": st.enabled, "fault": st.fault,
                "referenced": st.referenced, "target": st.target,
                "state": st.state_text,
                "min_in": st.cfg.min_in, "max_in": st.cfg.max_in,
            }
        return out

    def is_referenced(self, axis: str) -> bool:
        return self._state[axis.upper()].referenced

    # ── connect / disconnect ─────────────────────────────────────────────
    def connect(self) -> None:
        if self.connected:
            return
        if self.config.force_sim:
            self._serial = SimChain()
        else:
            import serial               # pyserial, imported lazily
            self._serial = serial.Serial(
                port=self.config.port, baudrate=P.BAUD,
                bytesize=P.DATA_BITS, parity=P.PARITY,
                stopbits=P.STOP_BITS, xonxoff=P.XONXOFF,
                timeout=0.05)
        self._state = {a: _AxisState(self.config.axis(a)) for a in AXES}
        self._t0 = time.monotonic()
        self.ring.clear()

        # probe each drive (MN); a silent unit is a connect failure —
        # better a loud refusal now than a dead axis mid-run
        try:
            for name, st in self._state.items():
                model = self._transact(st.cfg.unit, "MN")
                self._status(f"{name}: unit {st.cfg.unit} = {model}")
        except (ProtocolError, OSError) as exc:
            self.disconnect()
            raise ConnectionError(
                f"SmartStep chain probe failed: {exc} — check the COM "
                f"port, the daisy-chain wiring, and that every drive's "
                f"RS-232C echo is ON") from exc

        self._stop_evt.clear()
        self._monitor = threading.Thread(target=self._monitor_loop,
                                         name="lswt-traverse-monitor",
                                         daemon=True)
        self._monitor.start()
        self._status("connected"
                     + (" (SIMULATION)" if self.sim_mode else ""))

    def disconnect(self) -> None:
        self._stop_evt.set()
        if self._monitor is not None:
            self._monitor.join(timeout=3.0)
            self._monitor = None
        ser, self._serial = self._serial, None
        if ser is not None:
            try:
                # leave the rig stationary, whatever was in flight
                ser.write(P.command(None, "S"))
                ser.close()
            except OSError:
                pass
        self._status("disconnected")

    # ── transactions ─────────────────────────────────────────────────────
    def _send(self, unit: Optional[int], mnemonic: str,
              arg: str = "") -> None:
        """Fire-and-forget command (no reply expected)."""
        with self._io_lock:
            ser = self._serial
            if ser is None:
                raise RuntimeError("not connected")
            ser.reset_input_buffer()     # stale echo from prior sends
            ser.write(P.command(unit, mnemonic, arg))

    def _transact(self, unit: int, mnemonic: str, arg: str = "") -> str:
        """Command + wait for the ``*`` reply (echo discarded)."""
        with self._io_lock:
            ser = self._serial
            if ser is None:
                raise RuntimeError("not connected")
            ser.reset_input_buffer()
            ser.write(P.command(unit, mnemonic, arg))
            deadline = time.monotonic() + self.config.serial_timeout_s
            buf = bytearray()
            while time.monotonic() < deadline:
                chunk = ser.read(max(getattr(ser, "in_waiting", 0), 1))
                if chunk:
                    buf += chunk
                    payload = P.extract_response(bytes(buf))
                    if payload is not None:
                        return payload
                else:
                    time.sleep(0.005)
            raise ProtocolError(
                f"unit {unit}: no reply to {mnemonic}{arg} "
                f"within {self.config.serial_timeout_s:.1f}s")

    # ── referencing ("set current position to home") ─────────────────────
    def set_home(self, axis: str) -> None:
        """Declare the CURRENT spot the axis's home: ``SP<datum>``.

        Refused while the axis is moving — re-referencing a moving
        stage would race the drive's own position bookkeeping.
        """
        st = self._state[axis.upper()]
        if st.moving or st.jogging:
            raise RuntimeError(f"{axis}: stop the axis before setting "
                               f"home")
        datum = st.cfg.home_datum_in
        self._send(st.cfg.unit, "SP", P.format_real(datum))
        st.position = datum
        st.referenced = True
        st.state_text = "idle"
        self._status(f"{axis.upper()}: home set — position is now "
                     f"{datum:+.3f}\" and soft limits "
                     f"[{st.cfg.min_in:+.2f}, {st.cfg.max_in:+.2f}]\" "
                     f"are armed")

    def set_home_all(self) -> None:
        for name in AXES:
            self.set_home(name)

    # ── motion ───────────────────────────────────────────────────────────
    def move_to(self, x: Optional[float] = None,
                y: Optional[float] = None,
                z: Optional[float] = None) -> None:
        """Absolute move on any subset of axes (concurrent, non-blocking).

        Each target must be inside the axis's soft limits and the axis
        must be referenced — the two rules that replace homing.
        """
        requests = {"X": x, "Y": y, "Z": z}
        # validate everything BEFORE the first wire command: a compound
        # move must not start some axes and then refuse another
        for name, value in requests.items():
            if value is None:
                continue
            st = self._state[name]
            if not st.referenced:
                raise ValueError(
                    f"{name}: not referenced — jog to the reference "
                    f"spot and Set home first")
            if not (st.cfg.min_in <= value <= st.cfg.max_in):
                raise ValueError(
                    f"{name}: target {value:+.3f}\" outside soft limits "
                    f"[{st.cfg.min_in:+.2f}, {st.cfg.max_in:+.2f}]\"")
            if st.fault:
                raise RuntimeError(f"{name}: drive fault — {st.fault}")
        for name, value in requests.items():
            if value is None:
                continue
            st = self._state[name]
            cfg = st.cfg
            u = cfg.unit
            # a fresh command always supersedes whatever is in flight:
            # decel-stop, clear the FIFO, then queue the new profile —
            # otherwise a buffered move would QUEUE behind the old one
            self._send(u, "S")
            self._send(u, "CB")
            self._send(u, "AC", P.format_real(cfg.accel))
            self._send(u, "DE", P.format_real(cfg.accel))
            self._send(u, "VE", P.format_real(cfg.velocity_ips))
            self._send(u, "DA", P.format_real(float(value)))
            self._send(u, "GO")
            st.target = float(value)
            st.move_started = time.monotonic()
            st.moving = True
            st.jogging = 0
            st.state_text = f"→ {value:+.3f}\""
            self._status(f"{name}: moving to {value:+.3f}\"")

    def jog(self, axis: str, positive: bool) -> None:
        """Start a hold-to-jog: ``MC+ AC VE±v GO``; release = stop_axis.

        Always allowed on an unreferenced axis (how you REACH the
        reference spot). On a referenced axis a jog INTO an
        already-exceeded soft limit is refused, and the monitor stops
        any jog the moment it crosses one.
        """
        st = self._state[axis.upper()]
        cfg = st.cfg
        if st.fault:
            raise RuntimeError(f"{axis}: drive fault — {st.fault}")
        if st.referenced:
            if positive and st.position >= cfg.max_in:
                raise ValueError(f"{axis}: at the +{cfg.max_in:+.2f}\" "
                                 f"soft limit")
            if not positive and st.position <= cfg.min_in:
                raise ValueError(f"{axis}: at the {cfg.min_in:+.2f}\" "
                                 f"soft limit")
        sign = 1.0 if positive else -1.0
        u = cfg.unit
        self._send(u, "S")          # supersede any in-flight motion
        self._send(u, "CB")
        self._send(u, "MC", "+")
        self._send(u, "AC", P.format_real(cfg.accel))
        self._send(u, "DE", P.format_real(cfg.accel))
        self._send(u, "VE", P.format_real(sign * cfg.jog_velocity_ips))
        self._send(u, "GO")
        st.jogging = int(sign)
        st.target = None
        st.moving = True
        st.state_text = "jog +" if positive else "jog −"

    def stop_axis(self, axis: str) -> None:
        """Decel-stop one axis (addressed ``S``)."""
        st = self._state[axis.upper()]
        self._send(st.cfg.unit, "S")
        st.target = None
        st.jogging = 0
        st.state_text = "stopped"

    def stop_all(self) -> None:
        """Decel-stop the whole chain (broadcast ``S``) — the E-stop."""
        self._send(None, "S")
        for st in self._state.values():
            st.target = None
            st.jogging = 0
            st.state_text = "stopped"
        self._status("STOP — all axes halted")

    def kill_all(self) -> None:
        """Instant halt, NO decel ramp (broadcast ``K``). The manual
        warns this can shock the mechanics — panic use only."""
        self._send(None, "K")
        for st in self._state.values():
            st.target = None
            st.jogging = 0
            st.state_text = "KILLED"
        self._status("KILL — instant halt, no decel ramp")

    def set_energized(self, axis: str, on: bool) -> None:
        """Amplifier enable/disable (``EA1``/``EA0``)."""
        st = self._state[axis.upper()]
        self._send(st.cfg.unit, "EA", "1" if on else "0")
        st.enabled = on
        self._status(f"{axis.upper()}: amplifier "
                     + ("enabled" if on else "DISABLED"))

    # ── monitor loop ─────────────────────────────────────────────────────
    def _monitor_loop(self) -> None:
        while not self._stop_evt.wait(self.config.poll_s):
            try:
                self._poll_once()
            except (ProtocolError, OSError) as exc:
                self._status(f"poll failed: {exc}")
            except RuntimeError:
                return                     # disconnected under us

    def _poll_once(self) -> None:
        self._cycle += 1
        row = {"t": np.array([time.monotonic() - self._t0])}
        for name, st in self._state.items():
            pos = P.parse_position(self._transact(st.cfg.unit, "PA1"))
            sa = P.parse_status_hex(self._transact(st.cfg.unit, "SA1"))
            st.position = pos
            st.moving = bool(sa & P.SA_MOVING)
            row[name] = np.array([pos])
            self._supervise(name, st, sa)
            if self._cycle % max(self.config.drive_status_every, 1) == 0:
                sd = P.parse_status_hex(self._transact(st.cfg.unit, "SD1"))
                st.enabled = bool(sd & P.SD_ENABLED)
                faults = []
                if sd & P.SD_FOLLOWING_ERROR:
                    faults.append("following error")
                if sd & P.SD_OVER_CURRENT:
                    faults.append("over-current")
                if sd & P.SD_THERMAL_FAULT:
                    faults.append("thermal fault")
                if sd & P.SD_AMP_FAULT:
                    faults.append("amplifier fault (power cycle)")
                new_fault = ", ".join(faults)
                if new_fault and new_fault != st.fault:
                    self._status(f"{name}: DRIVE FAULT — {new_fault}")
                st.fault = new_fault
        self.ring.push_block(row)

    def _supervise(self, name: str, st: _AxisState, sa: int) -> None:
        """Per-tick reactions: move completion / timeout, soft-limit
        jog stop, hardware-limit latch reporting."""
        cfg = st.cfg
        # soft-limit reaction on a referenced jog: the drive knows
        # nothing about our limits, so the HOST is the fence
        if st.jogging and st.referenced:
            if ((st.jogging > 0 and st.position >= cfg.max_in)
                    or (st.jogging < 0 and st.position <= cfg.min_in)):
                self._send(cfg.unit, "S")
                st.jogging = 0
                st.state_text = "soft limit"
                self._status(f"{name}: jog stopped at the soft limit "
                             f"({st.position:+.3f}\")")
        # hardware limit switches, if wired: surface the latch
        if sa & (P.SA_LIMIT_NEG_LATCH | P.SA_LIMIT_POS_LATCH):
            side = "−" if sa & P.SA_LIMIT_NEG_LATCH else "+"
            if st.state_text != f"{side} limit switch":
                st.state_text = f"{side} limit switch"
                self._status(f"{name}: move terminated by the {side} "
                             f"hardware limit switch")
            st.target = None
            st.jogging = 0
            return
        # move completion / timeout
        if st.target is not None:
            done = (not st.moving
                    and abs(st.position - st.target) <= cfg.tolerance_in)
            if done or (sa & P.SA_MOVE_COMPLETE and not st.moving):
                st.target = None
                st.state_text = "idle"
                self._status(f"{name}: in position "
                             f"({st.position:+.3f}\")")
            elif (time.monotonic() - st.move_started
                    > self.config.move_timeout_s):
                self._send(cfg.unit, "S")
                st.target = None
                st.state_text = "TIMEOUT"
                self._status(f"{name}: move timed out — stopped at "
                             f"{st.position:+.3f}\"")
        elif not st.moving and not st.jogging \
                and st.state_text.startswith("→"):
            st.state_text = "idle"

    # ── helpers ──────────────────────────────────────────────────────────
    def wait_settled(self, timeout: float = 60.0) -> bool:
        """Block until no axis has an active target/jog (tests, scripts)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if all(st.target is None and not st.jogging and not st.moving
                   for st in self._state.values()):
                return True
            time.sleep(0.05)
        return False

    def _status(self, msg: str) -> None:
        log.info("%s", msg)
        if self.on_status:
            self.on_status(msg)
