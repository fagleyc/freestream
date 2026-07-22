"""TunnelMonitor — READ-ONLY view of the tunnel via the Red Lion gateway.

Polls Block1 (16 L4 elements, one contiguous 32-register FC3 read = an
atomic snapshot; 19 elements / 38 registers with
``config.bearing_temps``) at 1–2 Hz on a background thread. Publishes
:class:`~tunnel_plc.registers.TunnelSnapshot` objects with engineering
values and status booleans, keeps an RPM history ring, and manages the
connection: reconnect with exponential backoff after failures, and a
``stale`` flag on the snapshot once data is older than
``stale_after_s``.

This class has NO write methods of any kind — commanding the tunnel is
:class:`~tunnel_plc.control.TunnelControl`'s job, deliberately in a
separate file with its own arming requirements.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import time
from typing import Callable, Optional

import numpy as np

from .config import TunnelConfig
from .datamodel import ScanRingBuffer
from .emulator import SimGateway
from .gateway import GatewayError, ModbusGateway
from .registers import (BLOCK1_ADDR, BLOCK1_REGISTERS,
                        BLOCK1_REGISTERS_EXT, TunnelSnapshot,
                        decode_block1)

log = logging.getLogger(__name__)

FIELDS = ["t", "rpm_set", "actual_rpm", "fan_running", "inverter_fault"]


class TunnelMonitor:
    """Read-only Block1 poller with reconnect/backoff and staleness."""

    def __init__(self, config: Optional[TunnelConfig] = None,
                 gateway=None):
        self.config = config or TunnelConfig()

        self.on_status: Optional[Callable[[str], None]] = None
        self.on_snapshot: Optional[Callable[[TunnelSnapshot], None]] = None

        self.ring = ScanRingBuffer(FIELDS, capacity=100_000)

        # Shared transport: TunnelControl borrows this same gateway so
        # sim state (and the single TCP socket) is common to both.
        if gateway is not None:
            self.gateway = gateway
        elif self.config.force_sim:
            self.gateway = SimGateway(word_order=self.config.word_order,
                                      rpm_scale=self.config.rpm_scale)
        else:
            self.gateway = ModbusGateway(self.config.ip, self.config.port,
                                         self.config.unit_id,
                                         self.config.modbus_timeout_s,
                                         self.config.word_order)

        self._snapshot = TunnelSnapshot()
        self._snap_lock = threading.Lock()
        self._running = False
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._backoff = self.config.backoff_min_s
        self._consecutive_errors = 0

    # ── public state ─────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._running

    @property
    def sim_mode(self) -> bool:
        return isinstance(self.gateway, SimGateway)

    def snapshot(self) -> TunnelSnapshot:
        """The latest poll, with ``stale``/``age_s`` computed fresh."""
        with self._snap_lock:
            snap = dataclasses.replace(self._snapshot)
        if snap.t <= 0:
            snap.stale = True
            snap.age_s = float("inf")
        else:
            snap.age_s = time.time() - snap.t
            snap.stale = snap.age_s > self.config.stale_after_s
        return snap

    # ── lifecycle ────────────────────────────────────────────────────────
    def connect(self) -> None:
        if self._running:
            return
        self.gateway.connect()            # fail fast on a bad address
        self._poll_once()                 # first snapshot before returning
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._loop,
                                        name="tunnel-monitor", daemon=True)
        self._thread.start()
        self._running = True
        mode = "SIM" if self.sim_mode else "LIVE"
        snap = self.snapshot()
        self._status(f"Monitor connected ({mode}) — RPM "
                     f"{snap.actual_rpm:g}, fan "
                     f"{'RUNNING' if snap.fan_running else 'stopped'}")
        if not self.config.word_order_verified and not self.sim_mode:
            self._status("WARNING: 32-bit word order not yet verified "
                         "against a live nonzero value")

    def disconnect(self) -> None:
        if not self._running:
            return
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self.gateway.close()
        self._running = False
        self._status("Monitor disconnected")

    # ── poll loop ────────────────────────────────────────────────────────
    def _loop(self) -> None:
        while not self._stop_evt.is_set():
            t0 = time.perf_counter()
            try:
                if not self.gateway.connected:
                    self.gateway.connect()
                    self._status("Reconnected to gateway")
                self._poll_once()
                self._consecutive_errors = 0
                self._backoff = self.config.backoff_min_s
                wait = self.config.poll_s
            except GatewayError as exc:
                self._consecutive_errors += 1
                self.gateway.close()
                wait = self._backoff
                self._status(f"Poll failed ({self._consecutive_errors}): "
                             f"{exc} — retry in {wait:.0f}s")
                self._backoff = min(self._backoff * 2,
                                    self.config.backoff_max_s)
            elapsed = time.perf_counter() - t0
            self._stop_evt.wait(max(wait - elapsed, 0.02))

    def _poll_once(self) -> None:
        # default: 16 elements / 32 registers; with bearing_temps on the
        # SAME atomic read extends to elements 17–19 (38 registers).
        n_regs = (BLOCK1_REGISTERS_EXT if self.config.bearing_temps
                  else BLOCK1_REGISTERS)
        regs = self.gateway.read_registers(BLOCK1_ADDR, n_regs)
        values = decode_block1(regs, self.config.word_order,
                               self.config.rpm_scale,
                               bearing_cal=self.config.bearing_cal())
        snap = TunnelSnapshot(t=time.time(), stale=False, age_s=0.0,
                              raw_registers=tuple(regs), **values)
        with self._snap_lock:
            self._snapshot = snap
        self.ring.push_block({
            "t": np.array([snap.t]),
            "rpm_set": np.array([snap.rpm_set]),
            "actual_rpm": np.array([snap.actual_rpm]),
            "fan_running": np.array([float(snap.fan_running)]),
            "inverter_fault": np.array([float(snap.inverter_fault)]),
        })
        if self.on_snapshot:
            self.on_snapshot(snap)

    # ── helpers ──────────────────────────────────────────────────────────
    def _status(self, msg: str) -> None:
        log.info(msg)
        if self.on_status:
            self.on_status(msg)
