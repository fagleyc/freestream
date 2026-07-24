"""HeiseAdapter — HAL Streaming over heise.HeiseGauge (tunnel conditions).

Wraps the Heise PM digital indicator (RS-232 remote protocol): one
pressure port (TOTAL pressure — an absolute sensor, psia expected) and
one RTD port (temperature). The derived Mach/q chain and Streamlined
key on the exact channel names **Ptot** and **Temp** (Pdiff comes from
the NI DAQ in this mode), so the adapter canonicalises the driver's
port names by ROLE at build time and re-asserts them after every config
load: pressure → ``Ptot``, temperature → ``Temp``.

Unlike the DAQ front-ends there are no raw ADC volts here — the
indicator transmits calibrated engineering values (``?`` replies), so
:meth:`latest` AND :meth:`drain_block` both serve engineering units
(the configured pressure unit / the instrument-side RTD unit). The
indicator polls slowly (``poll_s``, ~4 Hz default), so drains naturally
yield few samples; the recorder resamples downstream, same as the
Tunnel group. ``sample_rate()`` is the honest poll rate and there is NO
``set_sample_rate`` — the serial indicator cannot follow the suite-wide
DAQ rate (same honesty rule as the ATE's fixed frame rate).

Drain cursor follows the driver's monotonic ``frame_count()`` clamped
to the ring's retained ``count``. No Time channel — the recorder owns
time. Sim runs against the heise emulator (ambient ~14.7 psi, ~72 deg).
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

from heise.config import (HeiseConfig, defaults_path,         # noqa: E402
                          load_startup_config)
from heise.device import HeiseGauge                           # noqa: E402

from ..derived import TUNNEL_CONDITION_CHANNELS               # noqa: E402
from ..hal import ChannelSpec, DeviceStatus, OFFLINE, OK      # noqa: E402
from ._configurable import ConfigurableAdapter                 # noqa: E402

GROUP = "Heise"

#: canonical channel name per port role (the derived chain / Streamlined
#: key on these EXACT names)
ROLE_NAMES = {"pressure": "Ptot", "temperature": "Temp"}

#: Heise port unit code → the ENGINEERING cal_unit vocabulary Streamlined
#: reduces against ("psia"/"degF"/"degC"/…). The total-pressure port is an
#: ABSOLUTE sensor, so its configured "psi" IS psia; the RTD unit (F vs C,
#: read from config — never hardcoded) maps to degF/degC. Anything already
#: in the target vocabulary passes through unchanged.
_CAL_UNIT = {"psi": "psia", "psia": "psia", "psid": "psid",
             "f": "degF", "degf": "degF",
             "c": "degC", "degc": "degC",
             "k": "K", "r": "degR", "ohms": "ohms"}


def _cal_unit(port_unit: str) -> str:
    """Engineering cal_unit for a Heise port's configured unit (read from
    the port config, NOT hardcoded — the F/C distinction is preserved)."""
    return _CAL_UNIT.get(str(port_unit).strip().lower(), str(port_unit))


def _canonicalise_port_names(cfg: HeiseConfig) -> None:
    """Force the canonical role → name mapping on the driver config
    (first pressure port → Ptot, first temperature port → Temp; an
    unexpected second port of the same role keeps a suffixed name so
    ring fields never collide)."""
    taken: set = set()
    for port in cfg.ports():
        want = ROLE_NAMES.get(port.role)
        if want is None:                    # role "off" — name is moot
            continue
        if want in taken:
            want = f"{want}2"
        port.name = want
        taken.add(want)


class HeiseAdapter(ConfigurableAdapter):
    """Streaming adapter for the Heise PM indicator (Ptot / Temp)."""

    id = "heise"
    label = "Heise PM (total pressure / temperature)"
    settings_dialog_path = ""      # device app has no standalone dialog
    comscan_module = "heise.comscan"
    comscan_hit_attr = "is_heise"

    def __init__(self, sim: bool = False,
                 config_path: Optional[str] = None):
        # Config provenance mirrors the standalone app: an explicit path
        # wins; otherwise a LIVE session starts from the device's own
        # startup defaults (defaults_path()) — the rig-proven COM port
        # in particular — while SIM stays on hermetic factory defaults.
        factory = True
        if config_path:
            cfg = HeiseConfig.load(config_path)
            factory = False
        elif sim:
            cfg = HeiseConfig()
        else:
            cfg = load_startup_config()
            factory = not defaults_path().exists()
        cfg.force_sim = bool(sim)
        if factory:
            # factory defaults: declare the units the derived chain expects
            # (psia total pressure; the RTD reads deg F — Casey's instrument
            # default; the config unit is the display/interpretation label
            # that Streamlined reduces against via cal_unit). Assign by ROLE
            # (never by left/right position) so it never puts a pressure code
            # on the RTD port regardless of which side each sensor is on.
            for port in cfg.ports():
                if port.role == "pressure":
                    port.unit = "psi"
                elif port.role == "temperature":
                    port.unit = "F"
        _canonicalise_port_names(cfg)
        self._cfg = cfg
        self._dev = HeiseGauge(cfg)
        self._sim = bool(sim)
        self._cursor = 0            # frame_count() at the last drain

    # ── ConfigurableAdapter ──────────────────────────────────────────────
    def apply_config_dict(self, data) -> None:
        """Generic apply (force_sim preserved by the mixin), then
        re-assert the canonical Ptot/Temp names — a saved bundle must
        never rename the channels the derived chain keys on."""
        super().apply_config_dict(data)
        _canonicalise_port_names(self._cfg)

    # ── DeviceBase ───────────────────────────────────────────────────────
    def connect(self) -> None:
        """Connect like the standalone flow: resolve the COM port first
        (blank port → one comscan), and if the configured port does not
        answer, one rescue scan before giving up — the embedded
        equivalent of the operator's Search button."""
        self.resolve_com_port()
        try:
            self._dev.connect()
        except Exception:
            if not self.rescue_com_port():
                raise
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
        """Honest poll rate (the ``?`` query period) — a slow serial
        indicator, deliberately NOT settable to the suite DAQ rate."""
        return float(self._dev.actual_hz
                     or 1.0 / max(self._cfg.poll_s, 0.05))

    #: canonical channel order — Ptot before Temp, regardless of the
    #: driver's own port order (the stock driver lists Temperature on
    #: port 1). Consumers key by NAME, but the declared order is what
    #: tables/recorded groups show — keep the historical Ptot, Temp.
    _ROLE_ORDER = {"pressure": 0, "temperature": 1}

    def channels(self) -> List[ChannelSpec]:
        # engineering values are streamed AND recorded (the indicator
        # transmits calibrated readings; there is no raw-volts layer)
        ports = sorted(self._cfg.enabled_ports(),
                       key=lambda p: self._ROLE_ORDER.get(p.role, 2))
        return [ChannelSpec(name=p.name, unit=p.unit, group=GROUP,
                            kind="tunnel", device_id=self.id)
                for p in ports]

    def tunnel_cal(self) -> Dict[str, Dict[str, object]]:
        """IDENTITY cal for the tunnel-condition ports (Ptot/Temp).

        The indicator transmits CALIBRATED engineering values, so the
        recorded arrays are already engineering units — Streamlined applies
        NO scaling (type="identity", slope 1, offset 0). ``cal_unit`` is the
        port's configured unit mapped to the engineering vocabulary (psia for
        the absolute total-pressure port; degF/degC read from the RTD port —
        never hardcoded)."""
        out: Dict[str, Dict[str, object]] = {}
        for p in self._cfg.enabled_ports():
            if p.name in TUNNEL_CONDITION_CHANNELS:
                out[p.name] = {"slope": 1.0, "offset": 0.0,
                               "unit": _cal_unit(p.unit), "type": "identity"}
        return out

    def latest(self) -> Dict[str, float]:
        """Engineering units (configured pressure unit / RTD unit)."""
        latest = self._dev.latest()
        if not latest:
            return {}
        return {p.name: latest[p.name]
                for p in self._cfg.enabled_ports() if p.name in latest}

    def drain_block(self) -> Dict[str, np.ndarray]:
        """All samples accumulated since the previous drain (engineering
        units — the Heise has no raw-volt fields)."""
        ring = self._dev.ring
        if ring is None:
            return {p.name: np.array([]) for p in
                    self._cfg.enabled_ports()}
        now = self._dev.frame_count()
        n = min(now - self._cursor, ring.count)
        self._cursor = now
        tail = ring.tail(n) if n > 0 else None
        out: Dict[str, np.ndarray] = {}
        for p in self._cfg.enabled_ports():
            out[p.name] = (tail[p.name]
                           if tail is not None and p.name in tail
                           else np.array([], dtype=np.float64))
        return out

    def raw_tail(self, n: int) -> Dict[str, np.ndarray]:
        """Last ``n`` samples per channel WITHOUT moving the drain
        cursor — for live monitors that must not steal recorder samples."""
        ring = self._dev.ring
        if ring is None or n <= 0:
            return {}
        tail = ring.tail(n)
        return {p.name: tail[p.name]
                for p in self._cfg.enabled_ports() if p.name in tail}
