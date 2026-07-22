"""Aero reduction — balance calibration → lift/drag/other aero forces.

This is the streamlined bridge between raw balance bridge-volts and the
wind-axis loads an operator wants to *see* live (never persisted — the
recorder stores raw only; §3.2). It composes:

* the StrainBook ``balcal`` pipeline (``read_vol_file`` → ``calc_coeffs`` →
  ``calc_brf_forces`` → ``element_utilization``) shipped with the device
  driver, and
* the body→wind rotation Streamlined uses in
  ``utils/windtunnel/transforms.py`` (reproduced here so Freestream never
  imports Streamlined at runtime and the two apps agree number-for-number)::

      Lift = cosα·Fz − sinα·Fx
      Drag = Fx·cosβ·cosα − Fy·sinβ + Fz·sinα·cosβ
      Side = Fx·sinβ·cosα + Fy·cosβ + Fz·sinα·sinβ
      Roll/Pitch/Yaw = Mx/My/Mz    (moments carried through)

Optionally forms coefficients ``CL = Lift/(q·S)`` etc. when a dynamic
pressure and model geometry are supplied. Also surfaces the per-element
utilisation vs the balance's rated maxima so the GUI can raise amber/red
overstress warnings and Freestream can refuse to record an overloaded
balance (§6.2).

Units follow the ``.vol`` file (lb / in·lb at the subsonic tunnel); pass
``q`` in psi and reference lengths in inches for dimensionless coefficients.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import numpy as np

# the balance-cal math lives with the StrainBook driver; make it importable
_DEVICES_DIR = Path(__file__).resolve().parents[1] / "devices"
if str(_DEVICES_DIR) not in sys.path:
    sys.path.insert(0, str(_DEVICES_DIR))

# balance channel names the reducer understands, in wire order
FORCE_CHANNELS = ("N1", "N2", "Y1", "Y2", "Axial", "Roll")
MOMENT_CHANNELS = ("AftPitch", "AftYaw", "FwdPitch", "FwdYaw", "Axial", "Roll")

_WIND_FIELDS = ("Lift", "Drag", "Side", "Roll", "Pitch", "Yaw")
_BODY_FIELDS = ("Fx", "Fy", "Fz", "Mx", "My", "Mz")
_COEF_FIELDS = ("CL", "CD", "CS", "Croll", "Cpitch", "Cyaw")


@dataclass
class Geometry:
    """Model reference geometry for coefficients (inches / sq-inches)."""
    S: float = 1.0          # reference area
    c: float = 1.0          # reference chord (pitch moments)
    b: float = 1.0          # reference span (roll/yaw moments)


@dataclass
class AeroResult:
    """One reduced block: body + wind loads, coefficients, utilisation."""
    body: Dict[str, np.ndarray] = field(default_factory=dict)
    wind: Dict[str, np.ndarray] = field(default_factory=dict)
    coeffs: Dict[str, np.ndarray] = field(default_factory=dict)
    elements: np.ndarray = field(default_factory=lambda: np.array([]))
    utilization: Dict[str, float] = field(default_factory=dict)
    overstress: bool = False
    warn: bool = False
    worst_channel: str = ""
    worst_util: float = 0.0

    def means(self) -> Dict[str, float]:
        """Scalar mean of every load/coefficient (for tiles + result rows)."""
        out: Dict[str, float] = {}
        for src in (self.wind, self.body, self.coeffs):
            for k, v in src.items():
                arr = np.asarray(v, dtype=float)
                if arr.size:
                    out[k] = float(np.mean(arr))
        return out


def load_balance_cal(vol_path, cal_type: str = "Linear"):
    """Read a ``.vol`` file and fit its calibration matrix.

    Returns a ``strainbook_616.balcal.BalanceCalibration`` with ``coeffs``
    populated. Raises on a missing/malformed file.
    """
    from strainbook_616 import balcal
    cal = balcal.read_vol_file(str(vol_path))
    balcal.calc_coeffs(cal, cal_type)
    return cal


def balance_summary(cal) -> str:
    from strainbook_616 import balcal
    return balcal.balance_summary(cal)


def _as_array(value, n: int) -> np.ndarray:
    arr = np.asarray(value, dtype=float)
    if arr.ndim == 0:
        return np.full(n, float(arr))
    return arr


def wind_axis(body: Dict[str, np.ndarray], alpha_deg, beta_deg=0.0
              ) -> Dict[str, np.ndarray]:
    """Rotate body-frame Fx/Fy/Fz/Mx/My/Mz into wind axes (Streamlined math)."""
    fx = np.asarray(body["Fx"], dtype=float)
    fy = np.asarray(body["Fy"], dtype=float)
    fz = np.asarray(body["Fz"], dtype=float)
    n = fx.size
    a = np.deg2rad(_as_array(alpha_deg, n))
    b = np.deg2rad(_as_array(beta_deg, n))
    ca, sa, cb, sb = np.cos(a), np.sin(a), np.cos(b), np.sin(b)
    return {
        "Lift": ca * fz - sa * fx,
        "Drag": fx * cb * ca - fy * sb + fz * sa * cb,
        "Side": fx * sb * ca + fy * cb + fz * sa * sb,
        "Roll": np.asarray(body["Mx"], dtype=float),
        "Pitch": np.asarray(body["My"], dtype=float),
        "Yaw": np.asarray(body["Mz"], dtype=float),
    }


def compute_aero(raw_volts: Dict[str, np.ndarray], cal, alpha_deg,
                 beta_deg=0.0, balance_config: str = "Force",
                 q: Optional[float] = None,
                 geom: Optional[Geometry] = None,
                 warn_utilization: float = 0.8) -> AeroResult:
    """Full reduction: raw balance volts + attitude → wind-axis loads.

    Parameters
    ----------
    raw_volts : per-channel bridge-volt arrays keyed by balance channel
        name (N1,N2,Y1,Y2,Axial,Roll or the moment-balance set), plus an
        optional ``Excitation`` array for normalisation.
    cal : a ``BalanceCalibration`` from :func:`load_balance_cal`.
    alpha_deg, beta_deg : attitude (scalar or per-sample array).
    q : dynamic pressure (psi) — supply with ``geom`` to also get CL/CD/…
    geom : model reference geometry.
    """
    from strainbook_616 import balcal
    brf = balcal.calc_brf_forces(raw_volts, cal,
                                 balance_config=balance_config)
    body = {f: np.asarray(getattr(brf, f), dtype=float) for f in _BODY_FIELDS}
    wind = wind_axis(body, alpha_deg, beta_deg)

    coeffs: Dict[str, np.ndarray] = {}
    if q is not None and geom is not None and q > 0:
        qS = q * geom.S
        if qS > 0:
            coeffs = {
                "CL": wind["Lift"] / qS,
                "CD": wind["Drag"] / qS,
                "CS": wind["Side"] / qS,
                "Croll": wind["Roll"] / (qS * geom.b),
                "Cpitch": wind["Pitch"] / (qS * geom.c),
                "Cyaw": wind["Yaw"] / (qS * geom.b),
            }

    util = balcal.element_utilization(cal, brf.elements)
    worst_name, worst = "", 0.0
    for name, u in util.items():
        if u > worst:
            worst_name, worst = name, u
    return AeroResult(
        body=body, wind=wind, coeffs=coeffs, elements=brf.elements,
        utilization=util, overstress=worst >= 1.0,
        warn=worst >= warn_utilization,
        worst_channel=worst_name, worst_util=worst)
