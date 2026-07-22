"""Calibration summary: run the least-squares reduction on a session and
render a plain-text report (the Python counterpart of the MATLAB
``FB_Cal_Summary`` publish step)."""

from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from typing import List, Optional

import numpy as np

from .daq import _ensure_devices_path
from .session import CalSession
from .volfile import vol_text


@dataclass
class CalSummary:
    cal: object                       # balcal.BalanceCalibration
    cal_type: str
    channels: List[str]
    r_squared: np.ndarray
    bias: np.ndarray
    coeffs: np.ndarray


def summarize(session: CalSession, cal_type: str = "Linear",
              vol_path: Optional[str] = None) -> CalSummary:
    """Reduce the session exactly as the consumers will: write (or reuse)
    the .vol, read it back with the driver's balcal parser, and fit."""
    _ensure_devices_path()
    from ni_usb_6351 import balcal

    if vol_path and os.path.exists(vol_path):
        path, cleanup = vol_path, False
    else:
        fd, path = tempfile.mkstemp(suffix=".vol")
        with os.fdopen(fd, "w", encoding="ascii", newline="\n") as f:
            f.write(vol_text(session))
        cleanup = True
    try:
        cal = balcal.read_vol_file(path)
        cal = balcal.calc_coeffs(cal, cal_type)
    finally:
        if cleanup:
            os.unlink(path)
    return CalSummary(cal=cal, cal_type=cal_type,
                      channels=list(cal.force_channels),
                      r_squared=cal.r_squared, bias=cal.bias,
                      coeffs=cal.coeffs)


def report_text(session: CalSession, summary: CalSummary) -> str:
    s = session
    lines = [
        "Balance Calibration Summary",
        "=" * 60,
        f"Balance type:      {s.kind.type_string}",
        f"Serial number:     {s.serial_number}",
        f"Outer diameter:    {s.outer_diameter}",
        f"Calibrated by:     {s.operator}",
        f"Date:              {s.cal_date.isoformat()}",
        f"Fit type:          {summary.cal_type}",
        f"Total test points: {s.point_count()}",
        "",
        "Points per orientation:",
    ]
    for o in s.orientations:
        n = len(s.points.get(o.key, []))
        if n:
            lines.append(f"  {o.section:<16s} {n:3d}")
    lines += ["", "Fit quality per element:",
              f"  {'Element':<10s} {'R^2':>10s} {'RMS bias':>12s}"]
    for i, name in enumerate(summary.channels):
        r2 = summary.r_squared[i] if i < summary.r_squared.size else float("nan")
        b = summary.bias[i] if i < summary.bias.size else float("nan")
        lines.append(f"  {name:<10s} {r2:10.6f} {b:12.4g}")
    lines += ["", f"Calibration matrix ({summary.coeffs.shape[0]}x"
                  f"{summary.coeffs.shape[1]}, Force = poly(V/Vex) @ C):"]
    hdr = "  " + " ".join(f"{n:>14s}" for n in summary.channels)
    lines.append(hdr)
    for row in np.atleast_2d(summary.coeffs):
        lines.append("  " + " ".join(f"{v:14.6e}" for v in row))
    lines.append("")
    return "\n".join(lines)
