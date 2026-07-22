"""StrainbookAdapter — HAL Streaming + Zeroable over strainbook_616.

Wraps the StrainBook/616 internal-balance driver. The driver's ring
buffer stores, per channel, the engineering value under the channel
name (mV for bridges) and the RAW voltage under ``<name>_V``:

* :meth:`latest` serves the engineering values (live UI),
* :meth:`drain_block` serves the RAW ``_V`` volts under the PLAIN
  channel name — the recorder persists raw only (spec §3.2/§5.3).

Draining uses a sample cursor against the driver's monotonically
increasing ``frame_count()`` (the ring's own ``count`` saturates at
capacity), clamped to what the ring still holds. ``zero()`` is the
driver's software ``tare(seconds)``. The recorder owns time — no Time
channel is exposed.
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

from strainbook_616.config import StrainbookConfig            # noqa: E402
from strainbook_616.device import Strainbook616               # noqa: E402

from ..hal import ChannelSpec, DeviceStatus, OFFLINE, OK      # noqa: E402
from ._configurable import ConfigurableAdapter                 # noqa: E402

GROUP = "StrainBook_0"


class StrainbookAdapter(ConfigurableAdapter):
    """Streaming + Zeroable adapter for the StrainBook/616 balance."""

    id = "strainbook"
    label = "StrainBook/616 (internal balance)"
    settings_dialog_path = "strainbook_616.app.settings_dialog:SettingsDialog"
    #: hardware classification, inherited into the recorded file markers
    #: (root attr ``balance_type``) by the sweep engine. NOTE: distinct
    #: from the CONFIG's ``balance_type`` (.vol balance-model metadata).
    balance_type = "internal"

    def __init__(self, sim: bool = False,
                 config_path: Optional[str] = None):
        cfg = (StrainbookConfig.load(config_path) if config_path
               else StrainbookConfig())
        cfg.force_sim = bool(sim)
        self._cfg = cfg
        self._dev = Strainbook616(cfg)
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

    # ── balance layout — SINGLE SOURCE OF TRUTH ──────────────────────────
    # The balance layout (Force|Moment) lives on the driver config and is
    # exposed here so the Freestream Forces panel and the embedded StrainBook
    # device panel share ONE value. Reading it drives the recorded channel
    # names (:meth:`channels`) and the live reduction; setting it renames the
    # four bridge channels on the live device (crescent-style rebind).
    @property
    def balance_config(self) -> str:
        return self._cfg.balance_config

    @balance_config.setter
    def balance_config(self, value: str) -> None:
        self._dev.set_balance_config(value)

    # ── balance calibration pointers — DEVICE-OWNED ──────────────────────
    # The .vol path and fit type are edited ONLY in the StrainBook device
    # panel's Forces tab (the single editor); Freestream's live Forces
    # readout INHERITS them through these read-only views each tick.
    @property
    def vol_path(self) -> str:
        return self._cfg.vol_path

    @property
    def cal_type(self) -> str:
        return self._cfg.cal_type

    def status(self) -> DeviceStatus:
        # report the DRIVER's actual sim state, not just the manager's
        # selection — if they ever diverge again the badge must say so
        # (a saved config bundle once silently re-armed force_sim on a
        # LIVE session; apply_config_dict now guards it, this is the
        # visible backstop)
        sim = self._sim or bool(self._cfg.force_sim)
        if not self._dev.connected:
            return DeviceStatus(state=OFFLINE, message="not connected",
                                sim=sim)
        return DeviceStatus(state=OK, sim=sim,
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
        # the mV/V engineering scaling is display-only.
        return [ChannelSpec(name=c.name, unit="V", group=GROUP,
                            kind="raw", device_id=self.id)
                for c in self._cfg.enabled_channels()]

    def latest(self) -> Dict[str, float]:
        """Engineering values (mV bridges / V excitation) for live UI."""
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

    def raw_tail(self, n: int) -> Dict[str, np.ndarray]:
        """Last ``n`` RAW-volt samples per channel WITHOUT moving the drain
        cursor — for the live Forces monitor (aero reduction), which must not
        steal samples from the recorder. Bridge channels come from the
        ``_V`` ring fields; the excitation channel keeps its engineering
        volts (0–10 V) so balcal can normalise by it.
        """
        ring = self._dev.ring
        if ring is None or n <= 0:
            return {}
        tail = ring.tail(n)
        out: Dict[str, np.ndarray] = {}
        for c in self._cfg.enabled_channels():
            key = f"{c.name}_V"
            if c.name == "Excitation" and "Excitation" in tail:
                out["Excitation"] = tail["Excitation"]
            elif key in tail:
                out[c.name] = tail[key]
        return out

    # ── Zeroable ─────────────────────────────────────────────────────────
    def zero(self, seconds: float = 0.5) -> Dict[str, float]:
        """Software tare on the current per-channel mean (volts)."""
        return self._dev.tare(seconds)

    @property
    def zero_count(self) -> int:
        """Bumps on every tare/clear-tare (any path: device panel button,
        sweep-engine zero) — peak-hold displays reset when it changes."""
        return int(getattr(self._dev, "tare_count", 0))
