"""Strainbook616 — threaded acquisition driver for the StrainBook/616.

Same lifecycle/callback shape as the other AeroVIS device drivers
(``connect``/``start``/``stop``/``disconnect``, ``on_status``/``on_block``,
``frame_count()``, device-owned ring buffer).

Hardware configuration happens at connect: per-channel WBK16 options
(bridge, filter, coupling, gain, SSH, inversion, output source), then the
standard continuous DaqX scan into a circular buffer drained by a poll
thread. The bridges run on an EXTERNAL excitation supply, so the driver
never commands the StrainBook's internal excitation DAC/banks — CH8 only
reads the external excitation back. ``config.force_sim`` swaps all of it
for a synthetic bridge-signal source.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np

from . import daqx
from .config import StrainChannelConfig, StrainbookConfig
from .datamodel import ScanRingBuffer, fields_for
from .emulator import SimCore

log = logging.getLogger(__name__)


class Strainbook616:
    """Threaded StrainBook/616 strain-bridge acquisition driver."""

    def __init__(self, config: Optional[StrainbookConfig] = None):
        self.config = config or StrainbookConfig()

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

        self._lib: Optional[daqx.DaqX] = None
        self._handle: Optional[int] = None
        self._buf = None
        self._buf_scans = 0
        self._chans: List[StrainChannelConfig] = []
        self._core: Optional[SimCore] = None

        # software tare (volts subtracted per channel before scaling);
        # tare_count bumps on every tare/clear so UI peak-hold displays
        # know to reset their history
        self._tare: Dict[str, float] = {}
        self.tare_count = 0

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

    # ── balance layout (Force ↔ Moment) ──────────────────────────────────
    def set_balance_config(self, balance_config: str) -> Dict[str, str]:
        """Switch the balance layout on the (possibly live) device.

        Delegates the four-bridge RENAME to the config, then keeps the
        running device coherent: the cached channel specs are the SAME
        objects the config renamed (so their names already moved), and the
        ring-buffer field keys + software-tare keys are remapped in place so
        the poll/sim loop and drain keep working without a reconnect. Mirrors
        the crescent ``set_config`` rebind pattern. Returns the applied
        ``{old_name: new_name}`` bridge map.
        """
        renames = self.config.set_balance_config(balance_config)
        if not renames:
            return renames
        # ring stores both "<name>" (engineering) and "<name>_V" (raw)
        field_map: Dict[str, str] = {}
        for old, new in renames.items():
            field_map[old] = new
            field_map[f"{old}_V"] = f"{new}_V"
        if self.ring is not None:
            self.ring.rename_fields(field_map)
        for old, new in renames.items():
            if old in self._tare:
                self._tare[new] = self._tare.pop(old)
        self._status(f"Balance layout → {balance_config} "
                     f"({', '.join(f'{o}→{n}' for o, n in renames.items())})")
        return renames

    def latest(self) -> Optional[Dict[str, float]]:
        return self.ring.latest() if self.ring is not None else None

    # ── software tare ────────────────────────────────────────────────────
    def tare(self, seconds: float = 0.5) -> Dict[str, float]:
        """Zero the bridge channels on their current mean (software tare)."""
        if self.ring is None:
            return {}
        n = max(10, int(seconds * max(self.actual_hz, 10.0)))
        tail = self.ring.tail(n)
        for ch in self._chans:
            if not ch.read_excitation and tail["t"].size:
                prior = self._tare.get(ch.name, 0.0)
                self._tare[ch.name] = prior + float(
                    np.mean(tail[f"{ch.name}_V"]))
        self.tare_count += 1
        self._status(f"Tared {len(self._tare)} channels over {seconds:.1f} s")
        return dict(self._tare)

    def clear_tare(self) -> None:
        self._tare = {}
        self.tare_count += 1
        self._status("Tare cleared")

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
                                            name="sb-sim", daemon=True)
            self._thread.start()
            self._connected = True
            self._status("Simulation mode — synthetic bridge signals")
            return

        self._sim = False
        self._open_hardware()
        self._thread = threading.Thread(target=self._poll_loop,
                                        name="sb-poll", daemon=True)
        self._thread.start()
        self._connected = True
        self._status(f"Acquiring '{self.config.device_name}' at "
                     f"{self.actual_hz:.1f} Hz × {len(self._chans)} channels")

    def _open_hardware(self) -> None:
        cfg = self.config
        self._lib = daqx.DaqX(cfg.dll_path or None)
        self._handle = self._lib.open(cfg.device_name)
        try:
            self._configure_channels()
            # front-panel CH n lives at DaqX channel n+8 (internal WBK16)
            channels = [c.channel + daqx.STRAIN_CHANNEL_OFFSET
                        for c in self._chans]
            gains = [daqx.WgcX1] * len(channels)
            # Per-channel polarity (live-verified 2026-07-16): bridge
            # channels scan BIPOLAR (±5 V input-referred, signed counts);
            # the 0–10 V excitation readback scans UNIPOLAR (0–10 V one
            # half-span down — _publish_counts adds ADC_FS_V back).
            base = (daqx.DafAnalog | daqx.DafUnsigned |
                    daqx.DafDifferential)
            flags = [base | (daqx.DafUnipolar if ch.read_excitation
                             else daqx.DafBipolar)
                     for ch in self._chans]
            self._lib.adc_set_scan(self._handle, channels, gains, flags)
            self._lib.adc_set_freq(self._handle, cfg.scan_hz)
            self.actual_hz = self._lib.adc_get_freq(self._handle) or \
                cfg.scan_hz
            self._lib.adc_set_acq(self._handle, daqx.DaamInfinitePost)
            self._lib.adc_set_trig(self._handle, daqx.DatsImmediate)

            self._buf_scans = max(int(cfg.buffer_seconds * self.actual_hz),
                                  1000)
            self._buf = self._lib.make_buffer(self._buf_scans,
                                              len(self._chans))
            self._lib.transfer_set_buffer(self._handle, self._buf,
                                          self._buf_scans)
            self._lib.transfer_start(self._handle)
            self._lib.arm(self._handle)
        except Exception:
            self._close_hardware()
            raise

    def _configure_channels(self) -> None:
        """Push per-channel WBK16 options (never internal excitation)."""
        lib, h = self._lib, self._handle
        for ch in self._chans:
            c = ch.channel + daqx.STRAIN_CHANNEL_OFFSET
            if ch.read_excitation:
                # External-excitation readback: the supply is wired to this
                # channel's differential INPUT, so read it as a plain
                # voltage at ×1 (full-bridge = 4-wire, no completion).
                # OUT_EXC_VOLTS is NOT used — live-verified 2026-07-16 that
                # it monitors only the INTERNAL banks (off on this rig) and
                # reads 0 V regardless of the external supply.
                lib.set_option(h, c, daqx.DcotWbk16OutSource,
                               daqx.OUT_SIGNAL)
                lib.set_option(h, c, daqx.DcotWbk16Bridge, ch.bridge)
                lib.set_option(h, c, daqx.DcotWbk16IAG, 0)
                lib.set_option(h, c, daqx.DcotWbk16PGA, 0)
            else:
                _total, iag, pga = ch.gain
                lib.set_option(h, c, daqx.DcotWbk16OutSource,
                               daqx.OUT_SIGNAL)
                lib.set_option(h, c, daqx.DcotWbk16Bridge, ch.bridge)
                lib.set_option(h, c, daqx.DcotWbk16IAG, iag)
                lib.set_option(h, c, daqx.DcotWbk16PGA, pga)
            lib.set_option(h, c, daqx.DcotWbk16FilterType, ch.filter_type)
            lib.set_option(h, c, daqx.DcotWbk16Couple,
                           daqx.COUPLE_AC if ch.ac_couple else daqx.COUPLE_DC)
            lib.set_option(h, c, daqx.DcotWbk16Inv,
                           daqx.INVERT_INVERTED if ch.invert
                           else daqx.INVERT_NORMAL)
            lib.set_option(h, c, daqx.DcotWbk16Sample,
                           daqx.SSH_ON if ch.ssh else daqx.SSH_BYPASSED)
            lib.set_option(h, c, daqx.DcotWbk16ShuntCal, daqx.SHUNT_NONE)

        # The bridges are powered by an EXTERNAL supply (the sole authority):
        # the driver NEVER commands the StrainBook's internal excitation DAC
        # or latches an internal excitation source. CH8 only READS the
        # external supply as a plain 0–10 V input (above).

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
        if not self._connected:
            raise RuntimeError("connect() before start()")
        self._running = True
        self._status("Publishing")

    def stop(self) -> None:
        self._running = False
        self._status("Idle")

    # ── poll loop (real hardware) ────────────────────────────────────────
    def _poll_loop(self) -> None:
        assert self._lib is not None and self._handle is not None
        n_ch = len(self._chans)
        raw = np.ctypeslib.as_array(self._buf).reshape(self._buf_scans, n_ch)
        last_total = 0
        t0_wall = time.time()
        period = self.config.poll_ms / 1000.0

        fifo_recoveries = 0
        while not self._stop.is_set():
            try:
                _active, total = self._lib.transfer_get_stat(self._handle)
            except daqx.DaqXError as exc:
                # FIFO Full (err 5) or a transient transfer fault: try to
                # recover by re-arming rather than killing acquisition.
                fifo_recoveries += 1
                if fifo_recoveries > 5:
                    self._status(f"Transfer failed repeatedly ({exc}) — "
                                 f"stopping acquisition")
                    break
                self._status(f"Transfer fault ({exc}) — re-arming "
                             f"(recovery {fifo_recoveries}/5)")
                if not self._rearm():
                    break
                last_total = 0
                self._stop.wait(period)
                continue
            new = total - last_total
            if new <= 0:
                self._stop.wait(period)
                continue
            if new > self._buf_scans:
                self._status(f"Buffer overrun ({new} scans); dropped "
                             f"{new - self._buf_scans}")
                last_total = total - self._buf_scans
                new = self._buf_scans
            idx = (np.arange(last_total, total) % self._buf_scans)
            counts = raw[idx, :].astype(np.float64)
            t = t0_wall + np.arange(last_total, total) / self.actual_hz
            last_total = total
            self._publish_counts(t, counts)
            self._stop.wait(period)

    def _rearm(self) -> bool:
        """Recover a stalled/overflowed transfer: stop → restart → re-arm.

        Returns False if recovery itself fails (caller should give up).
        """
        try:
            for op in (self._lib.disarm, self._lib.transfer_stop):
                try:
                    op(self._handle)
                except daqx.DaqXError:
                    pass
            self._lib.transfer_set_buffer(self._handle, self._buf,
                                          self._buf_scans)
            self._lib.transfer_start(self._handle)
            self._lib.arm(self._handle)
            return True
        except daqx.DaqXError as exc:
            self._status(f"Re-arm failed: {exc}")
            return False

    def _publish_counts(self, t: np.ndarray, counts: np.ndarray) -> None:
        block: Dict[str, np.ndarray] = {"t": t}
        for i, ch in enumerate(self._chans):
            total_gain = ch.gain[0]
            volts = daqx.counts_to_volts(counts[:, i], total_gain)
            if ch.read_excitation:
                # unipolar scan: 0–10 V arrives one half-span down
                volts = volts + daqx.ADC_FS_V
            volts = volts - self._tare.get(ch.name, 0.0)
            block[f"{ch.name}_V"] = volts
            block[ch.name] = ch.volts_to_eng(volts)
        self._emit(block, len(t))

    # ── sim loop ─────────────────────────────────────────────────────────
    def _sim_loop(self) -> None:
        assert self._core is not None
        dt = 1.0 / self.config.scan_hz
        period = self.config.poll_ms / 1000.0
        t0_wall = time.time()
        emitted = 0
        while not self._stop.is_set():
            due = int((time.time() - t0_wall) * self.config.scan_hz)
            n = due - emitted
            if n > 0:
                rel_t0 = emitted * dt
                volts = self._core.block(rel_t0, n, dt)
                block: Dict[str, np.ndarray] = {
                    "t": t0_wall + rel_t0 + np.arange(n) * dt}
                for ch in self._chans:
                    v = volts[ch.name] - self._tare.get(ch.name, 0.0)
                    block[f"{ch.name}_V"] = v
                    block[ch.name] = ch.volts_to_eng(v)
                emitted = due
                self._emit(block, n)
            self._stop.wait(period)

    def _emit(self, block: Dict[str, np.ndarray], n: int) -> None:
        self._scan_total += n
        if self.ring is not None:
            self.ring.push_block(block)
        if self._running and self.on_block:
            self.on_block(block)

    # ── helpers ──────────────────────────────────────────────────────────
    def _status(self, msg: str) -> None:
        log.info(msg)
        if self.on_status:
            self.on_status(msg)
