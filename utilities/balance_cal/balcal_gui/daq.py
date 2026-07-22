"""Device-agnostic DAQ layer for balance calibration.

Loads the analog-input drivers from ``../devices`` (NI USB-6351 primary,
StrainBook/616 alternate). Both drivers share the same surface —
``connect``/``start``/``stop``/``disconnect``, a ``ScanRingBuffer`` that
fills continuously from a daemon thread, ``actual_hz`` and per-channel
raw-volt fields ``<name>_V`` — so calibration only needs this thin shim:
configure the seven balance channels, then time-average fresh ring data
for each test point.

Volts are taken raw (``_V`` fields, never tared): the .vol format stores
absolute bridge voltages and the reduction removes zero offsets itself.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np

from .session import BalanceKind, elements_for

DEVICES_DIR = Path(__file__).resolve().parents[3] / "devices"

BACKENDS = {"ni6351": "NI USB-6351", "strainbook": "StrainBook/616"}


def _ensure_devices_path() -> None:
    p = str(DEVICES_DIR)
    if p not in sys.path:
        sys.path.insert(0, p)


def list_ni_devices() -> List[str]:
    """NI-MAX device aliases currently visible (empty if no NI-DAQmx)."""
    _ensure_devices_path()
    try:
        import nidaqmx.system
        return [d.name for d in nidaqmx.system.System.local().devices]
    except Exception:
        return []


@dataclass
class Acquisition:
    """One timed test-point acquisition (raw volts)."""
    t: np.ndarray = field(default_factory=lambda: np.array([]))
    volts: Dict[str, np.ndarray] = field(default_factory=dict)
    means: Dict[str, float] = field(default_factory=dict)
    stds: Dict[str, float] = field(default_factory=dict)
    seconds: float = 0.0
    rate_hz: float = 0.0


class BalanceDaq:
    """Owns (or wraps) one driver configured for balance calibration."""

    def __init__(self, backend: str = "ni6351", *,
                 device_name: str = "", sim: bool = False,
                 scan_hz: float = 0.0, driver=None):
        """``driver``: pass an already-connected NiUsb6351/Strainbook616
        (e.g. freestream's live device) to share it instead of opening a
        second session; lifecycle calls are then skipped."""
        self.backend = backend
        self._owns_driver = driver is None
        self.driver = driver if driver is not None else self._make_driver(
            backend, device_name, sim, scan_hz)
        self.on_status: Optional[Callable[[str], None]] = None
        self._abort = threading.Event()
        self.driver.on_status = self._status

    # ── construction ─────────────────────────────────────────────────────
    @staticmethod
    def _make_driver(backend: str, device_name: str, sim: bool,
                     scan_hz: float):
        _ensure_devices_path()
        if backend == "ni6351":
            from ni_usb_6351.config import NiDaqConfig
            from ni_usb_6351.device import NiUsb6351
            cfg = NiDaqConfig(force_sim=sim)
            if device_name:
                cfg.device_name = device_name
            if scan_hz:
                cfg.scan_hz = scan_hz
            return NiUsb6351(cfg)
        if backend == "strainbook":
            from strainbook_616.config import StrainbookConfig
            from strainbook_616.device import Strainbook616
            cfg = StrainbookConfig(force_sim=sim)
            if device_name:
                # hardware is opened by DaqX applet alias (device_name);
                # an IP-looking entry also lands in device_ip for info
                cfg.device_name = device_name
                if device_name.count(".") == 3:
                    cfg.device_ip = device_name
            if scan_hz:
                cfg.scan_hz = scan_hz
            return Strainbook616(cfg)
        raise ValueError(f"Unknown backend {backend!r} "
                         f"(use one of {sorted(BACKENDS)})")

    def _status(self, msg: str) -> None:
        if self.on_status:
            self.on_status(msg)

    # ── state ────────────────────────────────────────────────────────────
    @property
    def connected(self) -> bool:
        return bool(self.driver.connected)

    @property
    def sim_mode(self) -> bool:
        return bool(getattr(self.driver, "sim_mode", False))

    @property
    def actual_hz(self) -> float:
        return float(self.driver.actual_hz or 0.0)

    def bridge_channels(self, kind: BalanceKind) -> List[str]:
        """The six bridge-channel names, in element order."""
        return [el.channel for el in elements_for(kind)]

    def excitation_channel(self) -> str:
        return "Excitation"

    def all_channels(self, kind: BalanceKind) -> List[str]:
        return self.bridge_channels(kind) + [self.excitation_channel()]

    # ── lifecycle ────────────────────────────────────────────────────────
    def connect(self, kind: BalanceKind) -> None:
        """Apply the balance layout and open the device (if we own it)."""
        if not self._owns_driver and not self.connected:
            raise RuntimeError(
                "Shared device is not connected — connect it in the "
                "host application first")
        cfg_kind = "Force" if kind is BalanceKind.FORCE else "Moment"
        if self.connected:
            self.driver.set_balance_config(cfg_kind)
        else:
            self.driver.config.set_balance_config(cfg_kind)
        if self._owns_driver and not self.connected:
            self.driver.connect()
            self.driver.start()
        missing = [c for c in self.all_channels(kind)
                   if c not in self.driver.channel_names()]
        if missing:
            raise RuntimeError(
                f"Device is missing balance channels {missing}; "
                f"configured: {self.driver.channel_names()}")

    def disconnect(self) -> None:
        if self._owns_driver and self.connected:
            self.driver.disconnect()

    # ── acquisition ──────────────────────────────────────────────────────
    def abort_acquire(self) -> None:
        """Cancel an in-flight :meth:`acquire` (raises in its thread)."""
        self._abort.set()

    def acquire(self, seconds: float, kind: BalanceKind,
                settle_s: float = 0.0) -> Acquisition:
        """Blocking timed acquisition: wait for ``seconds`` of *fresh*
        samples (post button-press, so weight-swing transients from
        before the press are excluded), then average the raw volts.
        Call from a worker thread when driving a GUI.

        Volts are the driver's ``_V`` fields with any software tare
        added back: the .vol stores absolute bridge voltages and the
        reduction removes zero offsets itself, so a tare on a shared
        (e.g. freestream) device must not leak into the calibration.
        """
        if not self.connected:
            raise RuntimeError("Device not connected")
        self._abort.clear()
        if settle_s > 0:
            time.sleep(settle_s)
        start_frames = self.driver.frame_count()
        hz = max(self.actual_hz, 1.0)
        want = max(10, int(round(seconds * hz)))
        capacity = self.driver.ring.capacity if self.driver.ring else 0
        if capacity and want > capacity:
            raise RuntimeError(
                f"{seconds:g} s at {hz:g} Hz needs {want} samples but "
                f"the ring holds {capacity} — shorten the average or "
                f"lower the scan rate")
        deadline = time.time() + seconds + max(5.0, seconds)
        while self.driver.frame_count() - start_frames < want:
            if self._abort.is_set():
                raise RuntimeError("Acquisition cancelled")
            if time.time() > deadline:
                raise RuntimeError(
                    f"Timed out waiting for {want} samples "
                    f"({self.driver.frame_count() - start_frames} seen)")
            time.sleep(0.02)

        names = self.all_channels(kind)
        fields = ["t"] + [f"{n}_V" for n in names]
        data = self.driver.ring.tail(want, fields=fields)
        tare = dict(getattr(self.driver, "_tare", {}) or {})
        volts = {n: data[f"{n}_V"] + tare.get(n, 0.0) for n in names}
        means = {n: float(np.mean(v)) for n, v in volts.items()}
        stds = {n: float(np.std(v)) for n, v in volts.items()}
        return Acquisition(t=data["t"], volts=volts, means=means,
                           stds=stds, seconds=seconds, rate_hz=hz)

    def latest_volts(self, kind: BalanceKind) -> Dict[str, float]:
        latest = self.driver.latest() or {}
        return {n: latest.get(f"{n}_V", float("nan"))
                for n in self.all_channels(kind)}
