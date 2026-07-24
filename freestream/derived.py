"""Derived tunnel quantities — the ONE isentropic Mach/q module.

This is the composition layer: the device drivers stay raw/standalone by
project rule, and Freestream derives Mach/q from the tunnel-condition
channels (Pdiff [psid], Ptot [psia], Temp in the source instrument's unit —
deg C for the DaqBook thermocouple, deg F for the Heise RTD, normalized to
Celsius for the chain via :func:`temp_to_celsius`). The channels are found BY NAME across
the registry's streaming devices (:func:`tunnel_condition_sources`) —
all three on the SWT DaqBook, or split across devices as in the LSWT
mode (Pdiff on the NI DAQ, Ptot/Temp on the Heise). Every consumer
(live monitors, Tunnel dashboard, sweep /Tunnel derived channels, the
MachLoop targeting strategy) calls :func:`tunnel_state` here — one
source of truth.

The formulas mirror Streamlined's SSWT isentropic chain EXACTLY
(``utils/windtunnel/coefficients.py``, facility='SWT') so both apps agree
number-for-number::

    P0        = Ptot * PSI_TO_PA                     total (stagnation) [Pa]
    dP        = Pdiff * PSI_TO_PA                    pitot differential [Pa]
    P_static  = max(P0 - dP, 1)                      guard vs <= 0 [Pa]
    term      = max((P0/P_static)^((g-1)/g) - 1, 0)  isentropic core term
    M         = sqrt(2/(g-1) * term)
    q         = g/(g-1) * P_static * term            compressible dyn. press.
    T_static  = T0 / (1 + (g-1)/2 * M^2)
    rho       = P_static / (R_air * T_static)
    a         = sqrt(g * R_air * T_static)
    U         = M * a

(Replaces the old Red Lion HMI approximation that used q = Pdiff and a
fixed 343 m/s speed of sound.)

Derived values are NEVER persisted as reduced data — the recorder stores
them only in the /Tunnel convenience group with kind="derived"; raw
channels stay authoritative.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, Optional

# constants mirrored verbatim from Streamlined coefficients.py
PSI_TO_PA = 6894.75729
C_TO_K = 273.15
R_AIR = 287.058                  # J/(kg*K) - specific gas constant for air
GAMMA = 1.4                      # ratio of specific heats
G_RATIO = (GAMMA - 1.0) / GAMMA  # (gamma-1)/gamma = 2/7

#: the three engineering channels the isentropic chain needs, found BY
#: NAME across the registry's streaming devices (they historically all
#: lived on the DaqBook; in the LSWT mode Pdiff streams from the NI DAQ
#: while Ptot/Temp come from the Heise).
TUNNEL_CONDITION_CHANNELS = ("Pdiff", "Ptot", "Temp")


def tunnel_condition_sources(manager) -> Dict[str, object]:
    """``{channel: streaming adapter}`` for Pdiff/Ptot/Temp, found BY
    CHANNEL NAME across every streaming device in the registry.

    First device to declare a channel wins (registry/manifest order).
    The mapping is cached ON the manager — a DeviceManager's device set
    is fixed after construction, so one scan per registry suffices.
    Channels nobody declares are simply absent from the result; callers
    degrade exactly as before (q = None)."""
    cached = getattr(manager, "_tunnel_condition_sources", None)
    if cached is not None:
        return cached
    sources: Dict[str, object] = {}
    for dev in getattr(manager, "streaming", []):
        try:
            names = {ch.name for ch in dev.channels()}
        except Exception:                              # noqa: BLE001
            continue
        for name in TUNNEL_CONDITION_CHANNELS:
            if name in names:
                sources.setdefault(name, dev)
    try:
        manager._tunnel_condition_sources = sources
    except Exception:                                  # noqa: BLE001
        pass                                           # uncacheable manager
    return sources


def read_tunnel_conditions(manager) -> Dict[str, float]:
    """Latest ENGINEERING values (Pdiff psid / Ptot psia / Temp in its
    SOURCE unit — deg C for the DaqBook thermocouple, deg F for the Heise
    RTD) gathered across the registry via :func:`tunnel_condition_sources`.

    Temp is returned in the instrument's own unit (for display); the
    isentropic chain needs Celsius, so convert with :func:`temp_to_celsius`
    (:func:`temp_channel_unit` gives the source unit) before
    :func:`tunnel_state` — :func:`live_tunnel_state` already does this.

    Same-device fast path preserved: each source device's ``latest()``
    is called AT MOST ONCE per read (the SWT modes' single DaqBook stays
    one call). Returns whatever subset is actually available — channels
    with no source, no data yet, or a read error are simply missing."""
    sources = tunnel_condition_sources(manager)
    latest_by_dev: Dict[int, Dict[str, float]] = {}
    out: Dict[str, float] = {}
    for name, dev in sources.items():
        vals = latest_by_dev.get(id(dev))
        if vals is None:
            try:
                vals = dev.latest() or {}
            except Exception:                          # noqa: BLE001
                vals = {}
            latest_by_dev[id(dev)] = vals
        if name in vals:
            try:
                out[name] = float(vals[name])
            except (TypeError, ValueError):
                pass
    return out


def temp_channel_unit(manager) -> Optional[str]:
    """Engineering unit of the LIVE Temp channel, from its source device —
    'F'/'degF' for the Heise RTD, 'degC'/'C' for the DaqBook thermocouple.
    Prefers the adapter's injected cal_unit (authoritative), then the channel
    spec unit. None when there is no Temp source or its unit can't be read.

    The isentropic chain works in Celsius; a source that reports another unit
    (the Heise reads deg F) MUST be converted (see :func:`temp_to_celsius`),
    or T0/velocity/density are wrong and the display mislabels the tile."""
    dev = tunnel_condition_sources(manager).get("Temp")
    if dev is None:
        return None
    try:
        cal = dev.tunnel_cal() if hasattr(dev, "tunnel_cal") else {}
        unit = (cal.get("Temp") or {}).get("unit")
        if unit:
            return str(unit)
    except Exception:                                  # noqa: BLE001
        pass
    try:
        for spec in dev.channels():
            if getattr(spec, "name", None) == "Temp":
                return str(getattr(spec, "unit", "") or "") or None
    except Exception:                                  # noqa: BLE001
        pass
    return None


def temp_to_celsius(value: float, unit: Optional[str]) -> float:
    """Convert a temperature reading to Celsius for the isentropic chain.

    Recognizes F/degF, K/degK/kelvin, R/degR/rankine; anything else
    (C/degC/None) is assumed already Celsius. Keeps the ONE physics path in
    Celsius regardless of which instrument (Heise deg F vs DaqBook deg C)
    sources the Temp channel."""
    u = (str(unit or "").strip().lower()
         .replace("°", "").replace("deg", "").strip())
    v = float(value)
    if u in ("f", "fahrenheit"):
        return (v - 32.0) * 5.0 / 9.0
    if u in ("k", "kelvin"):
        return v - C_TO_K
    if u in ("r", "rankine"):
        return (v - 491.67) * 5.0 / 9.0
    return v


def temp_display_unit(unit: Optional[str]) -> str:
    """Display label for a Temp source unit — '°F' for the Heise RTD, '°C'
    for the DaqBook thermocouple, 'K'/'°R' otherwise. Keeps the T-total tile
    (and any temperature readout) labeled with the instrument's ACTUAL unit."""
    u = (str(unit or "").strip().lower()
         .replace("°", "").replace("deg", "").strip())
    if u in ("f", "fahrenheit"):
        return "°F"
    if u in ("k", "kelvin"):
        return "K"
    if u in ("r", "rankine"):
        return "°R"
    return "°C"


def live_tunnel_state(manager) -> Optional["TunnelState"]:
    """One-call derived flow condition from the live registry: find the
    Pdiff/Ptot/Temp sources, read them, run :func:`tunnel_state`.
    None when any of the three channels is unavailable (missing device,
    stream not started, read error) — the caller shows q = None exactly
    as the old DaqBook-only path did. When a state IS returned, check
    ``.valid`` as usual.

    The Temp reading is normalized to Celsius from its source unit first, so
    an LSWT Heise (deg F) yields correct T0/velocity/density, not an off-by
    ~30 K error from treating deg F as deg C."""
    vals = read_tunnel_conditions(manager)
    if any(k not in vals for k in TUNNEL_CONDITION_CHANNELS):
        return None
    temp_c = temp_to_celsius(vals["Temp"], temp_channel_unit(manager))
    return tunnel_state(vals["Pdiff"], vals["Ptot"], temp_c)


@dataclass
class TunnelState:
    """Derived flow condition (SSWT isentropic chain)."""
    mach: float = 0.0
    velocity_ms: float = 0.0         # U = M * a  (a from T_static)
    q_psi: float = 0.0               # compressible dynamic pressure [psi]
    q_pa: float = 0.0                # same, [Pa]
    p_static_pa: float = 0.0
    t_static_c: float = 0.0
    density_kgm3: float = 0.0        # STATIC density
    speed_of_sound_ms: float = 0.0
    valid: bool = False
    reason: str = ""


def mach_from_pressures(p_total: float, p_diff: float) -> float:
    """Isentropic pitot Mach from total and differential pressure [Pa].

    Mirrors Streamlined: ``P_static = max(P0 - dP, 1)`` then
    ``M = sqrt(2/(g-1) * max((P0/Ps)^((g-1)/g) - 1, 0))``.
    Raises ValueError when P0 <= 0 (no physical answer at all).
    """
    if p_total <= 0.0:
        raise ValueError("total pressure <= 0")
    p_static = max(p_total - p_diff, 1.0)
    term = max((p_total / p_static) ** G_RATIO - 1.0, 0.0)
    return math.sqrt((2.0 / (GAMMA - 1.0)) * term)


def mach_number(p_total: float, p_diff: float) -> float:
    """Unit-agnostic (ratio-based) isentropic Mach — both pressures in the
    SAME unit. Raises ValueError on non-physical inputs."""
    static = p_total - p_diff
    if static <= 0:
        raise ValueError("static pressure <= 0")
    term = (p_total / static) ** G_RATIO - 1.0
    if term < 0:
        raise ValueError("negative differential pressure")
    return math.sqrt((2.0 / (GAMMA - 1.0)) * term)


def tunnel_state(pdiff_psi: float, ptot_psi: float,
                 temp_c: float) -> TunnelState:
    """Full derived state from DaqBook engineering units; never raises —
    flags invalid instead. ``temp_c`` is the settling-chamber TOTAL
    temperature (the thermocouple reads T0)."""
    st = TunnelState()
    try:
        p0_pa = ptot_psi * PSI_TO_PA
        if p0_pa <= 0.0:
            raise ValueError("total pressure <= 0")
        dp_pa = pdiff_psi * PSI_TO_PA
        p_static = max(p0_pa - dp_pa, 1.0)          # guard, as Streamlined
        term = max((p0_pa / p_static) ** G_RATIO - 1.0, 0.0)
        gm1 = GAMMA - 1.0
        st.mach = math.sqrt((2.0 / gm1) * term)
        st.q_pa = (GAMMA / gm1) * p_static * term
        st.q_psi = st.q_pa / PSI_TO_PA
        t0_k = temp_c + C_TO_K
        if t0_k <= 0:
            raise ValueError("temperature below absolute zero")
        t_static_k = t0_k / (1.0 + 0.5 * gm1 * st.mach ** 2)
        st.p_static_pa = p_static
        st.t_static_c = t_static_k - C_TO_K
        st.density_kgm3 = p_static / (R_AIR * t_static_k)
        st.speed_of_sound_ms = math.sqrt(GAMMA * R_AIR * t_static_k)
        st.velocity_ms = st.mach * st.speed_of_sound_ms
        st.valid = True
    except (ValueError, ZeroDivisionError) as exc:
        st.valid = False
        st.reason = str(exc)
    return st
