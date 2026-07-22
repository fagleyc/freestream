"""NiUsb6351 — threaded acquisition driver for the NI USB-6351 (X series).

Same lifecycle/callback shape as the other AeroVIS device drivers
(``connect``/``start``/``stop``/``disconnect``, ``on_status``/``on_block``,
``frame_count()``, device-owned ring buffer).

Acquisition model (real hardware)
---------------------------------
One NI-DAQmx analog-input task: hardware-timed continuous sampling into the
DAQmx input buffer, drained by a poll thread via
:class:`~nidaqmx.stream_readers.AnalogMultiChannelReader`. The task's start
trigger comes from :class:`~ni_usb_6351.config.TriggerConfig` — immediate,
digital edge on a PFI line, or analog edge on APFI0 / a scanned AI channel.
With a hardware trigger the task arms at connect and the first samples
arrive only after the edge; the driver reports the armed→triggered
transition through ``on_status``.

Analog output: a second on-demand task holds static DC levels on the
enabled AO channels (``set_ao``); ``start_ao_wave``/``stop_ao_wave`` swap
it for a hardware-timed, regenerated waveform task built from each
channel's waveform settings.

When ``config.force_sim`` is set, a :class:`~ni_usb_6351.emulator.SimCore`
generates the stream instead — no NI-DAQmx install, no hardware.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, List, Optional

import numpy as np

from .config import AOChannelConfig, ChannelConfig, NiDaqConfig
from .datamodel import ScanRingBuffer, fields_for
from .emulator import SimCore

try:                                    # sim mode works without the module
    import nidaqmx
    from nidaqmx import stream_readers as _readers
    from nidaqmx.constants import AcquisitionType, Edge, Slope
    from nidaqmx.constants import TerminalConfiguration as _TC
    from nidaqmx.errors import DaqError
except ImportError:                     # pragma: no cover
    nidaqmx = None

    class DaqError(Exception):          # type: ignore[no-redef]
        pass

log = logging.getLogger(__name__)


def _terminal_const(terminal: str):
    """Map a config terminal string to the nidaqmx enum (version-safe)."""
    names = {"DIFF": ("DIFF", "DIFFERENTIAL"),
             "RSE": ("RSE",), "NRSE": ("NRSE",)}
    for attr in names.get(terminal, ()):
        if hasattr(_TC, attr):
            return getattr(_TC, attr)
    raise ValueError(f"Unknown terminal configuration {terminal!r}")


class NiUsb6351:
    """Threaded NI USB-6351 analog I/O driver."""

    def __init__(self, config: Optional[NiDaqConfig] = None):
        self.config = config or NiDaqConfig()

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
        self._task = None                       # AI task
        self._reader = None
        self._ao_task = None                    # static (on-demand) AO task
        self._ao_wave_task = None               # clocked waveform AO task
        self._chans: List[ChannelConfig] = []
        self._ao_chans: List[AOChannelConfig] = []
        self._triggered = False                 # first samples seen

        # sim state
        self._core: Optional[SimCore] = None

        # software tare (volts subtracted per balance channel)
        self._tare: Dict[str, float] = {}

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

    @property
    def waiting_for_trigger(self) -> bool:
        """Armed with a hardware trigger and no samples received yet."""
        return (self._connected and not self._triggered and
                self.config.trigger.mode != "immediate")

    @property
    def ao_wave_running(self) -> bool:
        return self._ao_wave_task is not None

    def frame_count(self) -> int:
        return self._scan_total

    def channel_names(self) -> List[str]:
        return [c.name for c in self._chans]

    def ao_names(self) -> List[str]:
        return [c.name for c in self._ao_chans]

    def latest(self) -> Optional[Dict[str, float]]:
        return self.ring.latest() if self.ring is not None else None

    # ── balance layout (Force ↔ Moment) ──────────────────────────────────
    def set_balance_config(self, balance_config: str) -> Dict[str, str]:
        """Switch the balance layout on the (possibly live) device.

        Delegates the four-bridge RENAME to the config, then keeps the
        running device coherent: the cached channel specs are the SAME
        objects the config renamed, and the ring-buffer field keys +
        software-tare keys are remapped in place so the poll/sim loop and
        drain keep working without a reconnect. Returns the applied
        ``{old_name: new_name}`` bridge map.
        """
        renames = self.config.set_balance_config(balance_config)
        if not renames:
            return renames
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

    # ── software tare ────────────────────────────────────────────────────
    def tare(self, seconds: float = 0.5) -> Dict[str, float]:
        """Zero the balance channels on their current mean (software tare)."""
        if self.ring is None:
            return {}
        n = max(10, int(seconds * max(self.actual_hz, 10.0)))
        tail = self.ring.tail(n)
        for ch in self._chans:
            if ch.balance and tail["t"].size:
                prior = self._tare.get(ch.name, 0.0)
                self._tare[ch.name] = prior + float(
                    np.mean(tail[f"{ch.name}_V"]))
        self._status(f"Tared {len(self._tare)} channels over {seconds:.1f} s")
        return dict(self._tare)

    def clear_tare(self) -> None:
        self._tare = {}
        self._status("Tare cleared")

    # ── lifecycle ────────────────────────────────────────────────────────
    def connect(self) -> None:
        if self._connected:
            return
        self._stop.clear()
        self._scan_total = 0
        self._triggered = False
        self._chans = self.config.enabled_channels()
        self._ao_chans = self.config.enabled_ao_channels()
        if not self._chans:
            raise RuntimeError("No enabled channels configured")
        self.ring = ScanRingBuffer(fields_for([c.name for c in self._chans]))

        if self.config.force_sim:
            self._sim = True
            self._core = SimCore(self._chans)
            self.actual_hz = self.config.scan_hz
            self._thread = threading.Thread(target=self._sim_loop,
                                            name="ni6351-sim", daemon=True)
            self._thread.start()
            self._connected = True
            self._status("Simulation mode — synthetic signals")
            return

        if nidaqmx is None:
            raise RuntimeError(
                "nidaqmx is not installed — pip install nidaqmx "
                "(or run with force_sim)")
        self._sim = False
        self._open_hardware()
        self._thread = threading.Thread(target=self._poll_loop,
                                        name="ni6351-poll", daemon=True)
        self._thread.start()
        self._connected = True
        trig = self.config.trigger
        if trig.mode == "immediate":
            self._status(f"Acquiring '{self.config.device_name}' at "
                         f"{self.actual_hz:.1f} Hz × "
                         f"{len(self._chans)} channels")
        else:
            self._status(f"Armed at {self.actual_hz:.1f} Hz — waiting for "
                         f"{trig.mode.replace('_', ' ')} on {trig.source}")

    def _open_hardware(self) -> None:
        cfg = self.config
        dev = cfg.device_name
        try:
            self._task = nidaqmx.Task(f"ni6351-ai-{dev}")
            for ch in self._chans:
                r = ch.native_range
                self._task.ai_channels.add_ai_voltage_chan(
                    f"{dev}/{ch.physical}",
                    name_to_assign_to_channel=ch.name,
                    terminal_config=_terminal_const(ch.terminal),
                    min_val=-r, max_val=r)
            buf_samps = max(int(cfg.buffer_seconds * cfg.scan_hz), 1000)
            self._task.timing.cfg_samp_clk_timing(
                rate=cfg.scan_hz,
                sample_mode=AcquisitionType.CONTINUOUS,
                samps_per_chan=buf_samps)
            self.actual_hz = float(self._task.timing.samp_clk_rate)
            self._configure_trigger(self._task)
            self._reader = _readers.AnalogMultiChannelReader(
                self._task.in_stream)
            self._task.start()
            self._open_ao()
        except Exception:
            self._close_hardware()
            raise

    def _configure_trigger(self, task) -> None:
        """Apply the configured AI start trigger to the task."""
        trig = self.config.trigger
        dev = self.config.device_name
        if trig.mode == "immediate":
            return
        if trig.mode == "digital_edge":
            edge = Edge.RISING if trig.edge == "rising" else Edge.FALLING
            task.triggers.start_trigger.cfg_dig_edge_start_trig(
                self._resolve_terminal(trig.source), trigger_edge=edge)
            return
        if trig.mode == "analog_edge":
            slope = (Slope.RISING if trig.edge == "rising"
                     else Slope.FALLING)
            src = trig.source
            if src.lower().startswith("ai"):
                # analog edge on a scanned AI channel: DAQmx wants the
                # VIRTUAL channel name of a channel in the task
                match = [c for c in self._chans if c.physical == src.lower()]
                if not match:
                    raise RuntimeError(
                        f"Analog trigger source {src} is not an enabled "
                        f"channel")
                src = match[0].name
            else:
                src = f"/{dev}/{src}"
            task.triggers.start_trigger.cfg_anlg_edge_start_trig(
                src, trigger_slope=slope, trigger_level=trig.level_v)
            return
        raise ValueError(f"Unknown trigger mode {trig.mode!r}")

    def _resolve_terminal(self, source: str) -> str:
        """'PFI0' → '/Dev2/PFI0'; pass fully-qualified terminals through."""
        if source.startswith("/"):
            return source
        return f"/{self.config.device_name}/{source}"

    # ── analog output ────────────────────────────────────────────────────
    def _open_ao(self) -> None:
        """Create the static AO task and drive the configured levels."""
        if not self._ao_chans:
            return
        dev = self.config.device_name
        self._ao_task = nidaqmx.Task(f"ni6351-ao-{dev}")
        for ch in self._ao_chans:
            self._ao_task.ao_channels.add_ao_voltage_chan(
                f"{dev}/{ch.physical}",
                name_to_assign_to_channel=f"ao_{ch.name}",
                min_val=ch.v_min, max_val=ch.v_max)
        self._write_static_ao()

    def _write_static_ao(self) -> None:
        vals = [ch.clamp(ch.static_v) for ch in self._ao_chans]
        data = vals[0] if len(vals) == 1 else vals
        self._ao_task.write(data, auto_start=True)

    def set_ao(self, name: str, volts: float) -> float:
        """Set a static DC level on one AO channel; returns the value sent.

        While a waveform is running the new level is stored in the config
        and applied when the waveform stops.
        """
        match = [c for c in self._ao_chans if c.name == name]
        if not match:
            raise ValueError(f"Unknown/disabled AO channel {name!r}")
        ch = match[0]
        ch.static_v = ch.clamp(volts)
        if self._sim or not self._connected:
            self._status(f"AO {name} = {ch.static_v:+.3f} V (sim)")
            return ch.static_v
        if self._ao_wave_task is not None:
            self._status(f"AO {name} = {ch.static_v:+.3f} V "
                         f"(deferred — waveform running)")
            return ch.static_v
        self._write_static_ao()
        self._status(f"AO {name} = {ch.static_v:+.3f} V")
        return ch.static_v

    def start_ao_wave(self) -> None:
        """Swap the static AO task for a regenerated waveform task.

        Each enabled AO channel plays its configured waveform (``none``
        holds the static level). Waveform frequencies snap to an integer
        number of cycles in the 1 s regeneration buffer so the loop point
        is seamless.
        """
        if self._sim or not self._connected:
            self._status("AO waveform started (sim)")
            return
        if not self._ao_chans:
            raise RuntimeError("No enabled AO channels")
        if self._ao_wave_task is not None:
            return
        cfg = self.config
        dev = cfg.device_name
        rate = float(cfg.ao_update_hz)
        n = max(int(round(rate)), 2)            # 1 s buffer
        t = np.arange(n) / rate
        rows = []
        descs = []
        for ch in self._ao_chans:
            f = max(1.0, round(ch.freq_hz))     # integer cycles per buffer
            phase = 2 * np.pi * f * t
            if ch.waveform == "sine":
                w = ch.offset_v + ch.amplitude_v * np.sin(phase)
            elif ch.waveform == "square":
                w = ch.offset_v + ch.amplitude_v * np.sign(np.sin(phase))
            elif ch.waveform == "triangle":
                w = ch.offset_v + ch.amplitude_v * (
                    2 / np.pi * np.arcsin(np.sin(phase)))
            else:                               # "none" → hold static level
                w = np.full(n, ch.static_v)
            rows.append(np.clip(w, ch.v_min, ch.v_max))
            descs.append(f"{ch.name}:{ch.waveform}"
                         + (f"@{f:.0f}Hz" if ch.waveform != "none" else ""))
        if self._ao_task is not None:
            self._ao_task.close()
            self._ao_task = None
        try:
            task = nidaqmx.Task(f"ni6351-ao-wave-{dev}")
            for ch in self._ao_chans:
                task.ao_channels.add_ao_voltage_chan(
                    f"{dev}/{ch.physical}",
                    min_val=ch.v_min, max_val=ch.v_max)
            task.timing.cfg_samp_clk_timing(
                rate=rate, sample_mode=AcquisitionType.CONTINUOUS,
                samps_per_chan=n)
            data = rows[0] if len(rows) == 1 else np.vstack(rows)
            task.write(data, auto_start=False)
            task.start()
        except Exception:
            try:
                task.close()
            except Exception:
                pass
            self._open_ao()                     # restore static levels
            raise
        self._ao_wave_task = task
        self._status(f"AO waveform running at {rate:.0f} S/s "
                     f"({', '.join(descs)})")

    def stop_ao_wave(self) -> None:
        """Stop the waveform and restore the static DC levels."""
        if self._ao_wave_task is None:
            if self._sim:
                self._status("AO waveform stopped (sim)")
            return
        try:
            self._ao_wave_task.stop()
        finally:
            self._ao_wave_task.close()
            self._ao_wave_task = None
        self._open_ao()
        self._status("AO waveform stopped — static levels restored")

    def zero_ao(self) -> None:
        """Drive all enabled AO channels to 0 V (stops any waveform)."""
        for ch in self._ao_chans:
            ch.static_v = 0.0
        if self._sim or not self._connected:
            self._status("AO zeroed (sim)")
            return
        if self._ao_wave_task is not None:
            self.stop_ao_wave()
        if self._ao_task is not None:
            self._write_static_ao()
        self._status("AO zeroed")

    # ── lifecycle (cont.) ────────────────────────────────────────────────
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
        for attr in ("_ao_wave_task", "_ao_task", "_task"):
            task = getattr(self, attr)
            if task is not None:
                for op in (task.stop, task.close):
                    try:
                        op()
                    except Exception:
                        pass
            setattr(self, attr, None)
        self._reader = None

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
        assert self._task is not None and self._reader is not None
        n_ch = len(self._chans)
        period = self.config.poll_ms / 1000.0
        t0_wall = 0.0
        recoveries = 0

        while not self._stop.is_set():
            try:
                avail = self._task.in_stream.avail_samp_per_chan
                if avail <= 0:
                    self._stop.wait(period)
                    continue
                data = np.empty((n_ch, avail), dtype=np.float64)
                got = self._reader.read_many_sample(
                    data, number_of_samples_per_channel=avail, timeout=2.0)
            except DaqError as exc:
                # input buffer overflow or a transient fault: recover by
                # restarting the task rather than killing acquisition
                recoveries += 1
                code = getattr(exc, "error_code", None) or exc
                if recoveries > 5:
                    self._status(f"Read failed repeatedly ({code}) — "
                                 f"stopping acquisition")
                    break
                self._status(f"Read fault — restarting task "
                             f"(recovery {recoveries}/5)")
                if not self._rearm():
                    break
                self._stop.wait(period)
                continue
            if got <= 0:
                self._stop.wait(period)
                continue
            if not self._triggered:
                self._triggered = True
                if self.config.trigger.mode != "immediate":
                    self._status("Trigger received — acquiring")
                t0_wall = time.time() - got / self.actual_hz
            t = t0_wall + (self._scan_total +
                           np.arange(got)) / self.actual_hz
            self._publish(t, data[:, :got])
            self._stop.wait(period)

    def _rearm(self) -> bool:
        """Recover a faulted/overflowed AI task: stop → start.

        Returns False if recovery itself fails (caller should give up).
        """
        try:
            self._task.stop()
            self._task.start()
            self._triggered = self.config.trigger.mode == "immediate"
            return True
        except DaqError as exc:
            self._status(f"Re-arm failed: {exc}")
            return False

    def _publish(self, t: np.ndarray, volts2d: np.ndarray) -> None:
        block: Dict[str, np.ndarray] = {"t": t}
        for i, ch in enumerate(self._chans):
            volts = volts2d[i] - self._tare.get(ch.name, 0.0)
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
