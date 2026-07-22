"""NiDaqAdapter — HAL Streaming + Zeroable over ni_usb_6351.

Wraps the NI USB-6351 force-balance DAQ driver. The driver's ring
buffer stores, per channel, the engineering value under the channel
name and the RAW voltage under ``<name>_V``:

* :meth:`latest` serves the engineering values (live UI),
* :meth:`drain_block` serves the RAW ``_V`` volts under the PLAIN
  channel name — the recorder persists raw only (spec §3.2/§5.3).

Draining uses a sample cursor against the driver's monotonically
increasing ``frame_count()`` (the ring's own ``count`` saturates at
capacity), clamped to what the ring still holds. ``zero()`` is the
driver's software ``tare(seconds)``. The recorder owns time — no Time
channel is exposed.

Channel layout (driver config defaults, ``ni_usb_6351.config``): the six
balance bridges on ai0..ai5 (N1/N2/Y1/Y2/Axial/Roll — Force layout), the
supplied-excitation readback on ai6, and the DIFFERENTIAL PRESSURE
transducer next (ai7). The adapter guarantees **Excitation** and
**Pdiff** are present-by-default (:func:`_ensure_default_channels`
repurposes the stock disabled ai7 "Spare" as Pdiff, re-asserted after
every config load) so the recorder always captures the excitation volts
and the derived Mach/q chain can find Pdiff by name. Balance channels
stream/record raw volts (unit "V"); non-balance extras report their
config unit (Pdiff psid) — ``latest()`` serves that engineering value
via the channel's scale/offset while ``drain_block`` stays raw volts.
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

from ni_usb_6351.config import ChannelConfig, NiDaqConfig     # noqa: E402
from ni_usb_6351.device import NiUsb6351                      # noqa: E402

from ..hal import ChannelSpec, DeviceStatus, OFFLINE, OK      # noqa: E402
from ._configurable import ConfigurableAdapter                 # noqa: E402

GROUP = "NI_USB_6351"


def _ensure_default_channels(cfg: NiDaqConfig) -> None:
    """Guarantee Excitation + Pdiff are present AND enabled.

    The new North-LSWT mode records the supplied excitation and the
    differential-pressure transducer alongside the six bridges. Driver
    defaults already put Excitation on ai6; Pdiff takes the next AI
    (the stock disabled ai7 "Spare" is repurposed, else the next free
    ai). Re-asserted after every config load so a saved bundle can
    never silently drop the channels the recorder/derived chain need.
    """
    by_name = {c.name: c for c in cfg.channels}

    exc = by_name.get("Excitation")
    if exc is None:
        cfg.channels.append(ChannelConfig(
            channel=_next_free_ai(cfg), name="Excitation",
            v_min=-10.0, v_max=10.0))
    else:
        exc.enabled = True

    pdiff = by_name.get("Pdiff")
    if pdiff is None:
        spare = next((c for c in cfg.channels
                      if c.name == "Spare" and not c.balance), None)
        if spare is not None:               # stock ai7 spare → Pdiff
            pdiff = spare
            pdiff.name = "Pdiff"
        else:
            pdiff = ChannelConfig(channel=_next_free_ai(cfg),
                                  name="Pdiff",
                                  v_min=-10.0, v_max=10.0)
            cfg.channels.append(pdiff)
        # transducer engineering-unit declaration; scale/offset stay the
        # operator's to calibrate (defaults record honest raw volts)
        pdiff.unit = "psid"
    pdiff.enabled = True
    pdiff.balance = False


def _next_free_ai(cfg: NiDaqConfig) -> int:
    used = {c.channel for c in cfg.channels}
    return next(i for i in range(64) if i not in used)


class NiDaqAdapter(ConfigurableAdapter):
    """Streaming + Zeroable adapter for the NI USB-6351 balance DAQ."""

    id = "ni_daq"
    label = "NI USB-6351 (DAQ)"
    settings_dialog_path = "ni_usb_6351.app.settings_dialog:SettingsDialog"
    #: hardware classification, inherited into the recorded file markers
    #: (root attr ``balance_type``) by the sweep engine: bridge VOLTS
    #: needing a .vol reduction. NOTE: distinct from the CONFIG's
    #: ``balance_type`` (.vol balance-model metadata).
    balance_type = "internal"

    def __init__(self, sim: bool = False,
                 config_path: Optional[str] = None):
        cfg = (NiDaqConfig.load(config_path) if config_path
               else NiDaqConfig())
        cfg.force_sim = bool(sim)
        _ensure_default_channels(cfg)
        self._cfg = cfg
        self._dev = NiUsb6351(cfg)
        self._sim = bool(sim)
        self._cursor = 0            # frame_count() at the last drain
        self._zero_count = 0        # bumps on every tare (peak-hold reset)

    # ── ConfigurableAdapter ──────────────────────────────────────────────
    def apply_config_dict(self, data) -> None:
        """Generic apply (force_sim preserved by the mixin), then
        re-assert the Excitation/Pdiff presence invariant — a saved
        bundle must never drop them from the channel list."""
        super().apply_config_dict(data)
        _ensure_default_channels(self._cfg)

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
    # exposed here so the Freestream Forces panel and the embedded NI DAQ
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
    # The .vol path and fit type live on the driver config (edited in the
    # NI device panel); Freestream's live Forces readout INHERITS them
    # through these read-only views each tick (mirrors strainbook).
    @property
    def vol_path(self) -> str:
        return self._cfg.vol_path

    @property
    def cal_type(self) -> str:
        return self._cfg.cal_type

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
        # Recorded values are the raw ADC volts (``_V`` ring fields).
        # Balance bridges declare "V" (scale forced to 1.0 anyway);
        # non-balance extras (Pdiff, Excitation) declare their config
        # unit — the unit latest() serves via the channel's scale/offset
        # (the derived q chain reads Pdiff [psid] from latest()).
        return [ChannelSpec(name=c.name,
                            unit="V" if c.balance else c.unit,
                            group=GROUP, kind="raw", device_id=self.id)
                for c in self._cfg.enabled_channels()]

    def latest(self) -> Dict[str, float]:
        """Engineering values (volts on bridges/excitation) for live UI."""
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
        volts so balcal can normalise by it.
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
        tares = self._dev.tare(seconds)
        self._zero_count += 1
        return tares

    @property
    def zero_count(self) -> int:
        """Bumps on every adapter-path tare — peak-hold displays reset
        when it changes (mirrors strainbook)."""
        return self._zero_count
