"""Compute + inject a balance ``.vol`` calibration into recorded metadata.

Shared by the INTERNAL-balance adapters (StrainBook/616, NI USB-6351). The
raw bridge volts still record VERBATIM — this only ADDS the computed
calibration (matrix, fit order, moment arms, serial) to
``/meta/devices/<balance_id>`` so Streamlined can reduce forces WITHOUT ever
opening the ``.vol`` itself. The ``vol_path``/``cal_type`` pointer keeps
being emitted alongside as harmless provenance.

Contract attrs emitted (exact names Streamlined reads):

* ``cal_matrix``    — float64 ndarray, shape ``(6*order, 6)`` (the
  least-squares cal matrix ``calc_coeffs(read_vol_file(vol)).coeffs``);
* ``cal_type``      — ``"Linear" | "Quadratic" | "Cubic"`` (the fit order);
* ``cal_distances`` — float64 ``[x1, x2, y1, y2]`` moment arms;
* ``balance_serial``— the balance serial (config, else the parsed ``.vol``);
* ``balance_type``  — ``"internal"`` (hardware classification).

The matrix is memoised per ``(module, vol path, cal_type, mtime)`` so it is
computed ONCE, not per test point. A missing/unreadable ``.vol`` or a failed
fit returns ``{}`` (logged) so a run is NEVER crashed by a bad cal file — the
caller still emits the string pointer separately.
"""

from __future__ import annotations

import logging
import os
from typing import Any, Dict

import numpy as np

log = logging.getLogger(__name__)

_ORDER = {"Linear": 1, "Quadratic": 2, "Cubic": 3}

#: (balcal module name, abs vol path, cal_type, mtime) -> (matrix, distances,
#: serial) | None.  ``None`` marks a known-bad file so it is not retried on
#: every point.
_MISS = object()
_CACHE: Dict[tuple, Any] = {}


def balance_cal_meta(balcal, vol_path: str, cal_type: str,
                     balance_serial: str = "",
                     balance_type: str = "internal") -> Dict[str, Any]:
    """Contract attrs to inject for an internal balance, or ``{}`` to skip.

    ``balcal`` is the device package's balcal module (must expose
    ``read_vol_file``, ``calc_coeffs`` and ``get_distance_values``). Never
    raises.
    """
    vol = str(vol_path or "")
    if not vol or not os.path.isfile(vol):
        return {}
    ctype = str(cal_type or "Linear")
    if ctype not in _ORDER:
        ctype = "Linear"
    try:
        mtime = os.path.getmtime(vol)
    except OSError:
        mtime = 0.0

    key = (getattr(balcal, "__name__", "balcal"),
           os.path.abspath(vol), ctype, mtime)
    cached = _CACHE.get(key, _MISS)
    if cached is _MISS:
        cached = _compute(balcal, vol, ctype, balance_serial)
        _CACHE[key] = cached
    if cached is None:
        return {}

    matrix, distances, serial = cached
    return {
        "cal_matrix": matrix,
        "cal_type": ctype,
        "cal_distances": distances,
        "balance_serial": serial,
        "balance_type": str(balance_type or "internal"),
    }


def _compute(balcal, vol: str, ctype: str, balance_serial: str):
    """Read + fit the ``.vol`` once; ``None`` on any failure (logged)."""
    try:
        cal = balcal.calc_coeffs(balcal.read_vol_file(vol), ctype)
        matrix = np.asarray(cal.coeffs, dtype=np.float64)
        dist = balcal.get_distance_values(cal)
        distances = np.array([dist["dx1"], dist["dx2"],
                              dist["dy1"], dist["dy2"]], dtype=np.float64)
        serial = (str(balance_serial or "").strip()
                  or str(cal.description.serial_number or "").strip())
        return matrix, distances, serial
    except Exception:                                  # noqa: BLE001
        log.exception("balance cal compute failed for %s (%s)", vol, ctype)
        return None
