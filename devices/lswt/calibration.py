"""LSWT motor-Hz ↔ tunnel-velocity calibration.

Ported VERBATIM from the deployed C# controller
``Tool_LSWT_Flow_Velocity\\HwControllerVelocityLSWT_ACB530.cs``:

* ``FPS_AT_HZ`` — the C# ``ftPerSecVelocityToMotorHertz`` table (lines
  59–65): a 61-point measured curve, index = motor Hz 0..60, value =
  tunnel velocity in ft/s. "Data obtained from LSWT experimental data"
  (the C# comment). 0 Hz → 0 ft/s, 60 Hz → 105.6851 ft/s.
* ``UNIT_MAXIMA`` — the C# ``SpeedUnitsConversion`` array (line 71):
  the tunnel's maximum speed expressed in each display unit
  {mps, fps, kph, Mach, mph}. The C# converted between units by the
  RATIO of these maxima.

Conversions here (documented choice):

* fps ↔ m/s, km/h, mph use the EXACT physical factors (0.3048 m per
  ft). They agree with the C# maxima ratios to <0.01% — the C# maxima
  are just the physical conversions of 105.6851 ft/s, slightly rounded
  — so the exact factors are preferred.
* fps ↔ Mach uses the C# maxima ratio (0.09466 / 105.6851), exactly as
  the C# did: Mach depends on the speed of sound at tunnel conditions,
  and 0.09466 is the deployed tool's calibrated value.

``hz_to_fps`` / ``fps_to_hz`` interpolate the measured table linearly
(``np.interp``; the table is strictly monotonic so the inverse is
well-defined) and CLAMP at the ends — no extrapolation past 60 Hz /
105.6851 ft/s.
"""

from __future__ import annotations

import numpy as np

# ── the 61-point measured table (C# lines 59–65, copied exactly) ────────
# index = motor Hz (0..60), value = tunnel velocity in ft/s
FPS_AT_HZ = np.array([
    0.0,       1.758977,  3.046637,  4.653816,  6.342082,
    8.250333,  10.10455,  11.79958,  13.62498,  15.33441,   # 0-9 Hz
    17.23438,  18.94476,  20.6633,   22.5259,   24.30955,
    26.08984,  27.81187,  29.43331,  31.46554,  33.09495,   # 10-19 Hz
    35.04737,  36.81266,  38.45688,  40.18791,  41.95815,
    43.90401,  45.7334,   47.29653,  49.12555,  51.1315,    # 20-29 Hz
    52.53426,  54.585,    56.06696,  57.93954,  59.67574,
    61.4636,   63.24984,  65.15342,  66.74848,  68.80276,   # 30-39 Hz
    70.40304,  71.66617,  74.12789,  75.86066,  77.39499,
    79.25162,  80.97028,  82.8775,   84.41257,  86.47657,   # 40-49 Hz
    87.96644,  89.8629,   91.39912,  93.07631,  94.23258,
    96.82365,  98.09353,  100.2771,  102.263,   103.4661,   # 50-59 Hz
    105.6851,                                               # 60 Hz
], dtype=np.float64)

_HZ_AXIS = np.arange(61, dtype=np.float64)

MAX_HZ = 60.0
MAX_FPS = float(FPS_AT_HZ[-1])          # 105.6851

# C# SpeedUnitsConversion (line 71): tunnel max speed per display unit
UNIT_MAXIMA = {
    "mps":  32.21282,
    "fps":  105.6851,
    "kph":  115.9661,
    "Mach": 0.09466,
    "mph":  72.06447,
}

UNITS = ("fps", "mps", "kph", "mph", "Mach")

# exact physical factors (1 ft = 0.3048 m); Mach via the C# maxima ratio
_FPS_TO_UNIT = {
    "fps":  1.0,
    "mps":  0.3048,
    "kph":  0.3048 * 3.6,               # 1.09728
    "mph":  3600.0 / 5280.0,            # 0.681818…
    "Mach": UNIT_MAXIMA["Mach"] / UNIT_MAXIMA["fps"],
}


def hz_to_fps(hz: float) -> float:
    """Tunnel velocity (ft/s) at a motor frequency (Hz), clamped 0–60."""
    return float(np.interp(hz, _HZ_AXIS, FPS_AT_HZ))


def fps_to_hz(fps: float) -> float:
    """Motor frequency (Hz) for a tunnel velocity (ft/s), clamped.

    The table is strictly monotonic increasing, so linear interpolation
    against the swapped axes is the exact inverse of ``hz_to_fps``.
    """
    return float(np.interp(fps, FPS_AT_HZ, _HZ_AXIS))


def fps_to_unit(fps: float, unit: str) -> float:
    """Convert ft/s into a display unit (see module docstring)."""
    try:
        return fps * _FPS_TO_UNIT[unit]
    except KeyError:
        raise ValueError(f"unknown unit {unit!r} (one of {UNITS})") from None


def unit_to_fps(value: float, unit: str) -> float:
    """Convert a display-unit value back into ft/s."""
    try:
        return value / _FPS_TO_UNIT[unit]
    except KeyError:
        raise ValueError(f"unknown unit {unit!r} (one of {UNITS})") from None
