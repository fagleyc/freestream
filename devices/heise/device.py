"""HeiseGauge — slim polled driver for the Heise PM digital indicator.

Same lifecycle/callback shape as the other suite drivers
(``connect``/``start``/``stop``/``disconnect``, ``on_block``/
``on_status``, device-owned ring buffer): a daemon thread queries ``?``
every ``poll_s`` and publishes one scan per reply with the enabled
ports' values under their configured channel names (default
``Pressure`` and ``Temperature``).

Units: pressure ports are remotely selectable (``set_pressure_unit`` /
EUNIT codes 0–12); an RTD (temperature) port's unit is chosen on the
instrument itself (F/C/K/Rankine/ohms) and carried in the config for
display. ``force_sim`` runs against the emulator with no hardware.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional, Union

import numpy as np

from .config import HeiseConfig, unit_code, unit_name
from .datamodel import ScanRingBuffer
from .emulator import SimSerial
from .protocol import HeiseError, HeiseProtocol

log = logging.getLogger(__name__)


class HeiseGauge:
    """Heise PM indicator over RS-232 (remote protocol)."""

    def __init__(self, config: Optional[HeiseConfig] = None):
        self.config = config or HeiseConfig()

        self.on_block: Optional[Callable[[Dict[str, np.ndarray]], None]] = None
        self.on_status: Optional[Callable[[str], None]] = None

        self.ring: Optional[ScanRingBuffer] = None
        self.actual_hz: float = 0.0

        self._proto: Optional[HeiseProtocol] = None
        self._connected = False
        self._running = False
        self._sim = False
        self._scan_total = 0
        self._errors = 0
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._cmd_lock = threading.RLock()

    # ── public state ─────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def running(self) -> bool:
        return self._running

    @property
    def sim_mode(self) -> bool:
        return self._sim

    def frame_count(self) -> int:
        return self._scan_total

    def channel_names(self) -> List[str]:
        return [p.name for p in self.config.enabled_ports()]

    def latest(self) -> Optional[Dict[str, float]]:
        return self.ring.latest() if self.ring is not None else None

    def _status(self, msg: str) -> None:
        log.info(msg)
        if self.on_status:
            self.on_status(msg)

    # ── lifecycle ────────────────────────────────────────────────────────
    def connect(self) -> None:
        if self._connected:
            return
        cfg = self.config
        self._sim = cfg.force_sim
        self._errors = 0
        if self._sim:
            self._proto = HeiseProtocol(SimSerial(cfg))
        else:
            self._proto = HeiseProtocol.open(cfg.com_port, cfg.baud,
                                             cfg.timeout_s)
        try:
            self._proto.clear_input()
            try:
                values = self._proto.read_values()  # probe
            except HeiseError:
                self._proto.resync()                # power-up stragglers
                values = self._proto.read_values()
            if cfg.apply_units_on_connect:
                self._apply_units()
        except HeiseError as exc:
            self._proto.close()
            self._proto = None
            raise HeiseError(
                f"Heise indicator not responding on {cfg.com_port} — "
                f"is it powered, in REMOTE protocol, at {cfg.baud} "
                f"baud? ({exc})") from exc

        names = self.channel_names()
        capacity = max(1000, int(cfg.buffer_seconds / max(cfg.poll_s,
                                                          0.05)))
        self.ring = ScanRingBuffer(["t"] + names, capacity=capacity)
        self.actual_hz = 1.0 / max(cfg.poll_s, 0.05)
        self._scan_total = 0
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._poll_loop,
                                        name="heise-poll", daemon=True)
        self._thread.start()
        self._connected = True
        mode = "SIM" if self._sim else f"LIVE on {cfg.com_port}"
        units = ", ".join(f"{p.name} [{p.unit}]"
                          for p in cfg.enabled_ports())
        self._status(f"Connected ({mode}) — {units}; "
                     f"first reading {values}")

    def disconnect(self) -> None:
        if not self._connected:
            return
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._proto is not None:
            self._proto.close()
            self._proto = None
        self._connected = False
        self._status("Disconnected")

    def start(self) -> None:
        if not self._connected:
            raise RuntimeError("connect() first")
        self._running = True

    def stop(self) -> None:
        self._running = False

    # ── units ────────────────────────────────────────────────────────────
    def _apply_units(self) -> None:
        """Push the configured pressure unit via EUNIT.

        Read-modify-write: an RTD (temperature) port REJECTS pressure
        unit codes — live 2026-07-23, 'EUNIT 0, 0' answered Err02
        because the left port is an RTD. Only the pressure port's code
        is changed; the other port keeps whatever the instrument
        reports. A units failure must never kill the connection —
        warn and fall back to the instrument's current setting.
        """
        cfg = self.config
        try:
            current = self._proto.get_units()
        except HeiseError as exc:
            self._status(f"Could not read engineering units ({exc}) — "
                         f"leaving the instrument as-is")
            return
        while len(current) < 2:
            current.append(0)
        desired = list(current)
        for idx, p in enumerate(cfg.ports()):
            if p.role == "pressure" and idx < 2:
                desired[idx] = unit_code(p.unit)
        if desired == current:
            return
        try:
            self._proto.set_units(desired[0], desired[1])
        except HeiseError as exc:
            self._status(f"Could not set the pressure unit ({exc}) — "
                         f"using the instrument's setting instead")
            for idx, p in enumerate(cfg.ports()):
                if p.role == "pressure" and idx < len(current):
                    p.unit = unit_name(current[idx])

    def set_pressure_unit(self, unit: Union[int, str],
                          port: str = "both") -> str:
        """Live pressure-unit change (name like ``"kPa"`` or EUNIT
        code). ``port``: 'left' | 'right' | 'both' — only pressure-role
        ports are changed. Returns the applied unit name."""
        if not self._connected:
            raise RuntimeError("connect() first")
        code = unit_code(unit)
        name = unit_name(code)
        cfg = self.config
        with self._cmd_lock:
            current = self._proto.get_units()
            while len(current) < 2:
                current.append(0)
            for idx, (p, tag) in enumerate(
                    ((cfg.left, "left"), (cfg.right, "right"))):
                if p.role == "pressure" and port in (tag, "both"):
                    current[idx] = code
                    p.unit = name
            self._proto.set_units(current[0], current[1])
        self._status(f"Pressure unit set to {name}")
        return name

    def get_unit_codes(self) -> List[int]:
        with self._cmd_lock:
            return self._proto.get_units()

    # ── instrument helpers ───────────────────────────────────────────────
    def zero(self, port: str = "both") -> None:
        """Zero the pressure port(s) (``ZERO``)."""
        with self._cmd_lock:
            self._proto.zero(port in ("left", "both"),
                             port in ("right", "both"))
        self._status(f"Zeroed {port}")

    def set_tare(self, left: bool, right: bool) -> None:
        with self._cmd_lock:
            self._proto.set_tare(left, right)

    def set_damping(self, level: int) -> None:
        with self._cmd_lock:
            self._proto.set_damping(level)

    def battery(self) -> float:
        with self._cmd_lock:
            return self._proto.battery()

    def read_now(self) -> Dict[str, float]:
        """One immediate ``?`` query mapped to channel names."""
        with self._cmd_lock:
            values = self._proto.read_values()
        return self._map_values(values)

    def _map_values(self, values: List[float]) -> Dict[str, float]:
        """Map the reply's values onto ports.

        When the indicator transmits a value per PHYSICAL port (the
        usual case — both ports active on the instrument), map by
        position so a driver-side disabled port doesn't shift its
        neighbour's value. A shorter reply feeds the enabled ports in
        order (indicator-side inactive port).
        """
        ports = self.config.ports()
        if len(values) >= len(ports):
            return {p.name: v for p, v in zip(ports, values)
                    if p.enabled}
        names = self.channel_names()
        if len(values) < len(names):
            raise HeiseError(
                f"expected {len(names)} value(s) for ports "
                f"{names}, got {values}")
        return dict(zip(names, values))

    # ── poll loop ────────────────────────────────────────────────────────
    def _poll_loop(self) -> None:
        cfg = self.config
        while not self._stop_evt.is_set():
            t0 = time.perf_counter()
            try:
                with self._cmd_lock:
                    if self._proto is None:
                        break
                    values = self._proto.read_values()
                mapped = self._map_values(values)
                self._errors = 0
                block = {"t": np.array([time.time()])}
                for name, v in mapped.items():
                    block[name] = np.array([v])
                self._scan_total += 1
                if self.ring is not None:
                    self.ring.push_block(block)
                if self._running and self.on_block:
                    try:
                        self.on_block(block)
                    except Exception:               # noqa: BLE001
                        log.exception("on_block callback failed")
            except HeiseError as exc:
                self._errors += 1
                self._status(f"Serial error ({self._errors}/"
                             f"{cfg.max_consecutive_errors}): {exc}")
                if self._proto is not None:
                    self._proto.resync()
                if self._errors >= cfg.max_consecutive_errors:
                    self._status("Serial watchdog tripped — "
                                 "check cable/power; disconnecting")
                    break
            elapsed = time.perf_counter() - t0
            self._stop_evt.wait(max(cfg.poll_s - elapsed, 0.01))
