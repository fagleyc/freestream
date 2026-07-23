"""Tunnel-speed units — the ONE entry/display unit layer.

The suite's canonical tunnel axis stays Mach (SweepPoint.mach, MachLoop,
Mach_cmd in the recorded files) — this module only translates what the
OPERATOR types and reads: the Measurement Setup speed unit, the speed
tolerance, the sweep-planner grid row and the operator-wait dialog all
speak one of :data:`SPEED_UNITS`; everything downstream keeps working in
canonical Mach.

Planning-time conversions (``mach_from`` / ``value_from_mach``) use a
FIXED standard-day speed of sound ``A0 = sqrt(gamma*R*288.15) ≈ 340.29
m/s`` — a NOMINAL map for turning a typed velocity into a canonical
Mach target, deliberately not the live speed of sound (a plan must not
change meaning with the weather). Measured comparisons
(:func:`measured_value`) DO use live conditions via
:func:`freestream.derived.live_tunnel_state`, so "at target" is judged
against the honest measured velocity. The ``rpm`` unit maps through
``config.rpm_per_mach`` — the same rig-tuned linear map the MachLoop
uses for its initial command.
"""

from __future__ import annotations

import math
from typing import Optional

from .derived import GAMMA, R_AIR, live_tunnel_state

#: the user-selectable tunnel-speed entry/display units
SPEED_UNITS = ("mach", "ft/s", "m/s", "rpm")

#: display labels (also the unit suffix in GUI captions)
LABELS = {"mach": "Mach", "ft/s": "ft/s", "m/s": "m/s", "rpm": "RPM"}

#: value display formats per unit (Mach to 3 places, velocities to one
#: decimal, RPM as a whole number with a thousands separator)
FORMATS = {"mach": "{:.3f}", "ft/s": "{:.1f}", "m/s": "{:.1f}",
           "rpm": "{:,.0f}"}

#: sensible |measured − target| bands per unit, applied when the
#: operator switches the unit in Measurement Setup (roughly equivalent
#: widths near the facilities' operating points)
DEFAULT_TOLERANCES = {"mach": 0.010, "ft/s": 2.0, "m/s": 0.5, "rpm": 10.0}

#: tolerance-spinbox hints per unit: (min, max, decimals, single step) —
#: the Measurement Setup dialog re-ranges its one tolerance spin from
#: these so a Mach band (0.001…) and an RPM band (…500) both fit.
SPIN_HINTS = {
    "mach": (0.001, 0.5, 3, 0.005),
    "ft/s": (0.1, 100.0, 1, 0.5),
    "m/s": (0.05, 50.0, 2, 0.1),
    "rpm": (1.0, 500.0, 0, 1.0),
}

#: sweep-planner speed-row placeholder hints per unit (all units keep
#: the auto-prepended air-off 0 — 0 means "air off" in every unit)
PLANNER_HINTS = {
    "mach": "e.g. 0.3  or  0.3,0.5,0.7  (air-off 0 added)",
    "ft/s": "e.g. 100  or  40:20:120  (air-off 0 added)",
    "m/s": "e.g. 30  or  10:5:35  (air-off 0 added)",
    "rpm": "e.g. 600  or  0:100:600  (air-off 0 added)",
}

#: compact axis symbols for the planner's run-book indicator strip
AXIS_SYMBOLS = {"mach": "M", "ft/s": "V", "m/s": "V", "rpm": "N"}

FT_PER_M = 1.0 / 0.3048            # exact international foot

#: FIXED standard-day speed of sound (T = 288.15 K) for PLANNING-TIME
#: velocity↔Mach conversion — nominal by design; measured comparisons
#: use the live isentropic chain instead. ≈ 340.29 m/s.
A0_MS = math.sqrt(GAMMA * R_AIR * 288.15)


def _check_unit(unit: str) -> None:
    if unit not in SPEED_UNITS:
        raise ValueError(f"unknown speed unit {unit!r}; "
                         f"expected one of {SPEED_UNITS}")


def mach_from(value: float, unit: str,
              rpm_per_mach: float = 1500.0) -> float:
    """Planning-time conversion: entered *value* in *unit* → canonical
    Mach (standard-day A0 for velocities; ``rpm_per_mach`` for RPM)."""
    _check_unit(unit)
    v = float(value)
    if unit == "mach":
        return v
    if unit == "m/s":
        return v / A0_MS
    if unit == "ft/s":
        return (v / FT_PER_M) / A0_MS
    # rpm — the MachLoop's own linear map, inverted
    rpm_per_mach = float(rpm_per_mach)
    if rpm_per_mach <= 0:
        raise ValueError("rpm_per_mach must be > 0 to convert RPM "
                         "targets to Mach")
    return v / rpm_per_mach


def value_from_mach(mach: float, unit: str,
                    rpm_per_mach: float = 1500.0) -> float:
    """Planning-time conversion: canonical Mach → the display *unit*
    (inverse of :func:`mach_from`; same nominal maps)."""
    _check_unit(unit)
    m = float(mach)
    if unit == "mach":
        return m
    if unit == "m/s":
        return m * A0_MS
    if unit == "ft/s":
        return m * A0_MS * FT_PER_M
    return m * float(rpm_per_mach)


def convert_velocity_ms(velocity_ms: float, unit: str) -> float:
    """A MEASURED velocity [m/s] in a velocity display unit (exact unit
    conversion — no nominal maps involved)."""
    if unit == "ft/s":
        return float(velocity_ms) * FT_PER_M
    return float(velocity_ms)


def measured_value(manager, setpoint, unit: str) -> Optional[float]:
    """The LIVE measured speed in *unit*, or None when unavailable.

    Mach and the velocity units come from the one isentropic chain
    (:func:`freestream.derived.live_tunnel_state` — live conditions, not
    the planning-time A0); RPM comes from the setpoint readback (the
    ``rpm`` key, else ``hz`` × 60 — the LSWT drive's equivalence)."""
    _check_unit(unit)
    if unit == "rpm":
        if setpoint is None:
            return None
        try:
            rb = setpoint.readback() or {}
        except Exception:                              # noqa: BLE001
            return None
        if "rpm" in rb:
            return float(rb["rpm"])
        if "hz" in rb:
            return float(rb["hz"]) * 60.0
        return None
    st = live_tunnel_state(manager)
    if st is None or not st.valid:
        return None
    if unit == "mach":
        return st.mach
    return convert_velocity_ms(st.velocity_ms, unit)
