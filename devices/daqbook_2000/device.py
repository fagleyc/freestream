"""Daqbook2000 — threaded acquisition driver for the DaqBook/2000 series.

Mirrors the lifecycle/callback shape of the other AeroVIS device drivers
(``connect``/``start``/``stop``/``disconnect``, ``on_status``/``on_block``
callbacks, ``frame_count()``), so it drops into the suite unchanged.

Acquisition model (real hardware)
---------------------------------
Continuous scan (``DaamInfinitePost``), immediate trigger, circular driver
buffer with per-sample update (``DatmCycleOn | DatmUpdateSingle``).  A poll
thread reads ``daqAdcTransferGetStat`` and lifts new scans out of the
circular buffer, converts counts → volts → engineering units, timestamps
them against the actual ADC clock, and pushes them into :attr:`ring`.

The device owns its :class:`~daqbook_2000.datamodel.ScanRingBuffer` so any
number of consumers (GUI, the balance app's AuxSource, AeroVIS sync) can
read the same stream.

When ``config.force_sim`` is set (or construction of the DLL fails and the
caller opts in), a :class:`~daqbook_2000.emulator.SimCore` generates the
stream instead — no DLL, no hardware.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np

from . import daqx
from .config import ChannelConfig, DaqbookConfig
from .datamodel import ScanRingBuffer, fields_for
from .emulator import SimCore

log = logging.getLogger(__name__)


class Daqbook2000:
    """Threaded DaqBook/2000-series analog-input driver."""

    def __init__(self, config: Optional[DaqbookConfig] = None):
        self.config = config or DaqbookConfig()

        # User callbacks (invoked from the IO thread — marshal to GUI thread).
        self.on_block: Optional[Callable[[Dict[str, np.ndarray]], None]] = None
        self.on_status: Optional[Callable[[str], None]] = None

        self.ring: Optional[ScanRingBuffer] = None
        self.actual_hz: float = 0.0

        self._connected = False
        self._running = False
        self._sim = False
        self._scan_total = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

        # hardware state
        self._lib: Optional[daqx.DaqX] = None
        self._handle: Optional[int] = None
        self._buf = None
        self._buf_scans = 0
        self._chans: List[ChannelConfig] = []

        # sim state
        self._core: Optional[SimCore] = None

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
        return [c.name for c in self._chans]

    def latest(self) -> Optional[Dict[str, float]]:
        return self.ring.latest() if self.ring is not None else None

    # ── lifecycle ────────────────────────────────────────────────────────
    def connect(self) -> None:
        if self._connected:
            return
        self._stop.clear()
        self._scan_total = 0
        self._chans = self.config.enabled_channels()
        if not self._chans:
            raise RuntimeError("No enabled channels configured")
        self.ring = ScanRingBuffer(fields_for([c.name for c in self._chans]))

        if self.config.force_sim:
            self._sim = True
            self._core = SimCore(self._chans)
            self.actual_hz = self.config.scan_hz
            self._thread = threading.Thread(target=self._sim_loop,
                                            name="daqbook-sim", daemon=True)
            self._thread.start()
            self._connected = True
            self._status("Simulation mode — synthetic tunnel signals")
            return

        self._sim = False
        self._open_hardware()
        self._thread = threading.Thread(target=self._poll_loop,
                                        name="daqbook-poll", daemon=True)
        self._thread.start()
        self._connected = True
        self._status(f"Acquiring '{self.config.device_name}' at "
                     f"{self.actual_hz:.1f} Hz × {len(self._chans)} channels")

    def _open_hardware(self) -> None:
        cfg = self.config
        self._lib = daqx.DaqX(cfg.dll_path or None)
        self._handle = self._lib.open(cfg.device_name)
        try:
            channels = [c.channel for c in self._chans]
            gains, flags = [], []
            for c in self._chans:
                gain, bipolar = c.gain_bipolar
                gains.append(daqx.GAIN_CODE[gain])
                flags.append(daqx.build_channel_flags(c.differential, bipolar))
            self._lib.adc_set_scan(self._handle, channels, gains, flags)
            self._lib.adc_set_freq(self._handle, cfg.scan_hz)
            self.actual_hz = self._lib.adc_get_freq(self._handle) or cfg.scan_hz
            self._lib.adc_set_acq(self._handle, daqx.DaamInfinitePost)
            self._lib.adc_set_trig(self._handle, daqx.DatsImmediate)

            self._buf_scans = max(int(cfg.buffer_seconds * self.actual_hz), 1000)
            self._buf = self._lib.make_buffer(self._buf_scans, len(self._chans))
            self._lib.transfer_set_buffer(self._handle, self._buf,
                                          self._buf_scans)
            self._lib.transfer_start(self._handle)
            self._lib.arm(self._handle)
        except Exception:
            self._close_hardware()
            raise

    def disconnect(self) -> None:
        if not self._connected:
            return
        self._running = False
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if not self._sim:
            self._close_hardware()
        self._connected = False
        self._sim = False
        self._status("Disconnected")

    def _close_hardware(self) -> None:
        if self._lib is not None and self._handle is not None:
            for op in (self._lib.disarm, self._lib.transfer_stop):
                try:
                    op(self._handle)
                except daqx.DaqXError:
                    pass
            try:
                self._lib.close(self._handle)
            except daqx.DaqXError:
                pass
        self._handle = None
        self._buf = None
        self._lib = None

    def start(self) -> None:
        """Begin publishing blocks (acquisition streams continuously)."""
        if not self._connected:
            raise RuntimeError("connect() before start()")
        self._running = True
        self._status("Publishing")

    def stop(self) -> None:
        self._running = False
        self._status("Idle")

    # ── real-hardware poll loop ──────────────────────────────────────────
    def _poll_loop(self) -> None:
        assert self._lib is not None and self._handle is not None
        n_ch = len(self._chans)
        raw = np.ctypeslib.as_array(self._buf).reshape(self._buf_scans, n_ch)
        last_total = 0
        t0_wall = time.time()
        period = self.config.poll_ms / 1000.0

        while not self._stop.is_set():
            try:
                _active, total = self._lib.transfer_get_stat(self._handle)
            except daqx.DaqXError as exc:
                self._status(f"Transfer status failed: {exc}")
                break
            new = total - last_total
            if new <= 0:
                self._stop.wait(period)
                continue
            if new > self._buf_scans:          # overrun: keep newest buffer
                self._status(f"Buffer overrun ({new} scans); "
                             f"dropped {new - self._buf_scans}")
                last_total = total - self._buf_scans
                new = self._buf_scans
            idx = (np.arange(last_total, total) % self._buf_scans)
            counts = raw[idx, :].astype(np.float64)
            t = t0_wall + np.arange(last_total, total) / self.actual_hz
            last_total = total
            self._publish(t, counts)
            self._stop.wait(period)

    def _publish(self, t: np.ndarray, counts: np.ndarray) -> None:
        block: Dict[str, np.ndarray] = {"t": t}
        for i, ch in enumerate(self._chans):
            gain, bipolar = ch.gain_bipolar
            volts = daqx.counts_to_volts(counts[:, i], gain, bipolar)
            block[f"{ch.name}_V"] = volts
            block[ch.name] = ch.volts_to_eng(volts)
        self._scan_total += len(t)
        if self.ring is not None:
            self.ring.push_block(block)
        if self._running and self.on_block:
            self.on_block(block)

    # ── simulation loop ──────────────────────────────────────────────────
    def _sim_loop(self) -> None:
        assert self._core is not None
        dt = 1.0 / self.config.scan_hz
        period = self.config.poll_ms / 1000.0
        t0_wall = time.time()
        emitted = 0
        while not self._stop.is_set():
            elapsed = time.time() - t0_wall
            due = int(elapsed * self.config.scan_hz)
            n = due - emitted
            if n > 0:
                rel_t0 = emitted * dt
                volts = self._core.block(rel_t0, n, dt)
                t = t0_wall + rel_t0 + np.arange(n) * dt
                counts_like: Dict[str, np.ndarray] = {"t": t}
                for ch in self._chans:
                    v = volts[ch.name]
                    counts_like[f"{ch.name}_V"] = v
                    counts_like[ch.name] = ch.volts_to_eng(v)
                emitted = due
                self._scan_total += n
                if self.ring is not None:
                    self.ring.push_block(counts_like)
                if self._running and self.on_block:
                    self.on_block(counts_like)
            self._stop.wait(period)

    # ── helpers ──────────────────────────────────────────────────────────
    def _status(self, msg: str) -> None:
        log.info(msg)
        if self.on_status:
            self.on_status(msg)
