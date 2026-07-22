"""DaqbookAdapter — HAL Streaming over daqbook_2000 (tunnel conditions).

Wraps the always-on DaqBook/2000 tunnel-conditions stream (Pdiff, Ptot,
Temp). The driver's ring stores both the engineering value (psid/psia/
degC via each channel's scale/offset) under the channel name and the
RAW voltage under ``<name>_V``:

* :meth:`latest` serves engineering units for the live UI,
* :meth:`drain_block` serves the RAW ``_V`` volts under the PLAIN
  channel name — raw-only capture, reduced later by Streamlined.

Drain cursor follows the driver's monotonic ``frame_count()`` clamped
to the ring's retained ``count`` (the ring count saturates at
capacity). No Time channel — the recorder owns time.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

_DEVICES_DIR = Path(__file__).resolve().parents[2] / "devices"
if str(_DEVICES_DIR) not in sys.path:
    sys.path.insert(0, str(_DEVICES_DIR))

from daqbook_2000.config import DaqbookConfig                 # noqa: E402
from daqbook_2000.device import Daqbook2000                   # noqa: E402

from ..hal import ChannelSpec, DeviceStatus, OFFLINE, OK      # noqa: E402
from ._configurable import ConfigurableAdapter                 # noqa: E402

GROUP = "DaqBook2005"


class DaqbookAdapter(ConfigurableAdapter):
    """Streaming adapter for the DaqBook/2000 tunnel conditions."""

    id = "daqbook"
    label = "DaqBook/2000 (tunnel conditions)"
    settings_dialog_path = "daqbook_2000.app.settings_dialog:SettingsDialog"

    def __init__(self, sim: bool = False,
                 config_path: Optional[str] = None):
        cfg = (DaqbookConfig.load(config_path) if config_path
               else DaqbookConfig())
        cfg.force_sim = bool(sim)
        self._cfg = cfg
        self._dev = Daqbook2000(cfg)
        self._sim = bool(sim)
        self._cursor = 0            # frame_count() at the last drain

    # ── DeviceBase ───────────────────────────────────────────────────────
    def connect(self) -> None:
        self._dev.connect()
        self._cursor = self._dev.frame_count()

    def disconnect(self) -> None:
        self._dev.disconnect()

    @property
    def connected(self) -> bool:
        return self._dev.connected

    @property
    def sim(self) -> bool:
        return self._sim

    def status(self) -> DeviceStatus:
        if not self._dev.connected:
            return DeviceStatus(state=OFFLINE, message="not connected",
                                sim=self._sim)
        return DeviceStatus(state=OK, sim=self._sim,
                            last_sample_age_s=self._sample_age())

    def _sample_age(self) -> Optional[float]:
        latest = self._dev.latest()
        if not latest:
            return None
        return max(time.time() - latest["t"], 0.0)

    # ── Streaming ────────────────────────────────────────────────────────
    def start(self) -> None:
        self._dev.start()

    def stop(self) -> None:
        self._dev.stop()

    def sample_rate(self) -> float:
        return float(self._dev.actual_hz or self._cfg.scan_hz)

    def set_sample_rate(self, hz: float) -> None:
        """Adopt the suite-wide sample rate (the driver reads ``scan_hz``
        when acquisition starts, so this applies at the next connect)."""
        self._cfg.scan_hz = float(hz)

    def channels(self) -> List[ChannelSpec]:
        # Recorded values are the raw ADC volts (``_V`` ring fields);
        # psid/psia/degC scaling is applied only in latest().
        return [ChannelSpec(name=c.name, unit="V", group=GROUP,
                            kind="tunnel", device_id=self.id)
                for c in self._cfg.enabled_channels()]

    def latest(self) -> Dict[str, float]:
        """Engineering units (psid/psia/degC) for the live UI."""
        latest = self._dev.latest()
        if not latest:
            return {}
        return {c.name: latest[c.name]
                for c in self._cfg.enabled_channels()}

    def drain_block(self) -> Dict[str, np.ndarray]:
        """All RAW-volt samples accumulated since the previous drain."""
        ring = self._dev.ring
        if ring is None:
            return {c.name: np.array([]) for c in
                    self._cfg.enabled_channels()}
        now = self._dev.frame_count()
        n = min(now - self._cursor, ring.count)
        self._cursor = now
        tail = ring.tail(n) if n > 0 else None
        out: Dict[str, np.ndarray] = {}
        for c in self._cfg.enabled_channels():
            out[c.name] = (tail[f"{c.name}_V"] if tail is not None
                           else np.array([], dtype=np.float64))
        return out
