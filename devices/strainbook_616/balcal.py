"""Balance calibration (.vol) and body-frame force computation.

Self-contained copy of the relevant parts of Streamlined's
``utils/windtunnel/calibration.py`` and ``transforms.py`` (same math, same
file format, same results) so this package stays standalone. Keep the
algorithms in sync with Streamlined if they ever change there.

Pipeline: ``read_vol_file`` → ``calc_coeffs`` (least-squares cal matrix) →
``calc_brf_forces`` (bridge volts / excitation → element loads → body-frame
Fx, Fy, Fz, Mx, My, Mz using the balance distances). Units follow the .vol
file (lb / in·lb at the subsonic tunnel).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np


# ─────────────────────────────────────────────────────────────────────────
#  Data structures (mirroring Streamlined)
# ─────────────────────────────────────────────────────────────────────────

@dataclass
class BalanceDescription:
    balance_type: str = ""
    serial_number: str = ""
    outer_diameter: str = ""


@dataclass
class MaxLoads:
    values: Dict[str, float] = field(default_factory=dict)
    units: Dict[str, str] = field(default_factory=dict)


@dataclass
class Distances:
    values: Dict[str, float] = field(default_factory=dict)


@dataclass
class BalanceCalibration:
    info: Dict[str, str] = field(default_factory=dict)
    description: BalanceDescription = field(
        default_factory=BalanceDescription)
    max_loads: MaxLoads = field(default_factory=MaxLoads)
    distances: Distances = field(default_factory=Distances)
    force_channels: List[str] = field(default_factory=list)
    row_indices: List[int] = field(default_factory=list)
    force: np.ndarray = field(default_factory=lambda: np.array([]))
    volts: np.ndarray = field(default_factory=lambda: np.array([]))
    coeffs: np.ndarray = field(default_factory=lambda: np.array([]))
    force_est: np.ndarray = field(default_factory=lambda: np.array([]))
    cal_type: str = "Linear"
    r_squared: np.ndarray = field(default_factory=lambda: np.array([]))
    bias: np.ndarray = field(default_factory=lambda: np.array([]))
    file: str = ""


@dataclass
class BRFForces:
    Fx: np.ndarray = field(default_factory=lambda: np.array([]))
    Fy: np.ndarray = field(default_factory=lambda: np.array([]))
    Fz: np.ndarray = field(default_factory=lambda: np.array([]))
    Mx: np.ndarray = field(default_factory=lambda: np.array([]))
    My: np.ndarray = field(default_factory=lambda: np.array([]))
    Mz: np.ndarray = field(default_factory=lambda: np.array([]))
    elements: np.ndarray = field(default_factory=lambda: np.array([]))


@dataclass
class Geometry:
    C: float = 1.0
    S: float = 1.0
    b: float = 1.0
    mshift: np.ndarray = field(
        default_factory=lambda: np.array([0.0, 0.0, 0.0]))
    flip: bool = False


# ─────────────────────────────────────────────────────────────────────────
#  .vol parsing (verbatim Streamlined behaviour)
# ─────────────────────────────────────────────────────────────────────────

def _parse_line(line: str) -> Tuple[Optional[str], Optional[str]]:
    line = line.strip()
    parts = line.split('-->')
    if len(parts) > 1:
        key = re.sub(r'\s+', '', parts[0])
        value = parts[1].strip()
        return key, value
    return None, None


def read_vol_file(filepath: str) -> BalanceCalibration:
    """Read a force balance voltage calibration file (.vol)."""
    cal = BalanceCalibration()
    cal.file = str(filepath)

    channel_data = []

    with open(filepath, 'r') as f:
        current_field = 'Info'
        in_header = True

        for line in f:
            line = line.strip()
            if not line:
                continue

            if line.startswith('['):
                field_name = re.sub(r'[\[\]\s]', '', line)
                current_field = field_name
                in_header = False

                if 'pos' in field_name.lower() or \
                        'neg' in field_name.lower():
                    channel = field_name[:-3]
                    multiplier = 1 if 'pos' in field_name.lower() else -1

                    next_line = next(f).strip()
                    _, nloads_str = _parse_line(next_line)
                    nloads = int(nloads_str)
                    next(f)                     # skip column-header line

                    data = []
                    for _ in range(nloads):
                        data_line = next(f).strip()
                        values = [float(v) for v in data_line.split(',')]
                        data.append(values)

                    channel_data.append({
                        'multiplier': multiplier,
                        'channel': channel,
                        'nloads': nloads,
                        'data': np.array(data)
                    })
                continue

            if in_header:
                key, value = _parse_line(line)
                if key:
                    cal.info[key] = value

            elif current_field == 'BalanceDescription':
                key, value = _parse_line(line)
                if key:
                    key_lower = key.lower()
                    if 'type' in key_lower:
                        cal.description.balance_type = value
                    elif 'serial' in key_lower:
                        cal.description.serial_number = value
                    elif 'diameter' in key_lower:
                        cal.description.outer_diameter = value

            elif current_field == 'MaximalBalanceLoads':
                key, value = _parse_line(line)
                if key:
                    clean_key = re.sub(r'[()]', '', key)
                    parts = value.split()
                    cal.max_loads.values[clean_key] = float(parts[0])
                    if len(parts) > 1:
                        cal.max_loads.units[clean_key] = parts[1]

            elif current_field == 'Distances':
                key, value = _parse_line(line)
                if key:
                    cal.distances.values[key] = float(value)

    if channel_data:
        total_loads = sum(cd['nloads'] for cd in channel_data)
        n_channels = 6

        force = np.zeros((total_loads, n_channels))
        volts = np.zeros((total_loads, n_channels))

        row_idx = 0
        current_channel = channel_data[0]['channel']
        col = 0
        cal.force_channels.append(current_channel)
        cal.row_indices.append(row_idx)

        for cd in channel_data:
            if cd['channel'] != current_channel:
                col += 1
                current_channel = cd['channel']
                cal.force_channels.append(current_channel)
                cal.row_indices.append(row_idx)

            data = cd['data']
            nloads = cd['nloads']
            multiplier = cd['multiplier']

            force[row_idx:row_idx + nloads, col] = \
                np.abs(data[:, 0]) * multiplier

            zero_indices = np.where(data[:, 0] == 0)[0]
            if len(zero_indices) > 0:
                zero_offset = np.mean(data[zero_indices, 1:7], axis=0)
            else:
                zero_offset = np.zeros(6)

            volts[row_idx:row_idx + nloads, :] = \
                (data[:, 1:7] - zero_offset) / data[:, 7:8]

            row_idx += nloads

        cal.force = force
        cal.volts = volts

    return cal


def form_higher_order_terms(v: np.ndarray, order: int) -> np.ndarray:
    if order == 1:
        return v
    elif order == 2:
        return np.hstack([v, v * v])
    elif order == 3:
        return np.hstack([v, v * v, v * v * v])
    raise ValueError(f"Order {order} not supported. Use 1, 2, or 3.")


def calc_coeffs(cal: BalanceCalibration,
                cal_type: str = 'Linear') -> BalanceCalibration:
    """Least-squares cal matrix: Force = poly(Volts) @ Coeffs."""
    if cal.force.size == 0 or cal.volts.size == 0:
        raise ValueError("Force and Volts data must be populated first")

    order_map = {'Linear': 1, 'Quadratic': 2, 'Cubic': 3}
    order = order_map.get(cal_type, 1)

    volts_poly = form_higher_order_terms(cal.volts, order)
    coeffs, _res, _rank, _s = np.linalg.lstsq(volts_poly, cal.force,
                                              rcond=None)
    force_est = volts_poly @ coeffs

    ss_res = np.sum((cal.force - force_est) ** 2, axis=0)
    ss_tot = np.sum((cal.force - np.mean(cal.force, axis=0)) ** 2, axis=0)
    r_squared = 1 - ss_res / ss_tot
    bias = np.sqrt(np.sum((cal.force - force_est) ** 2, axis=0) /
                   len(cal.force))

    cal.coeffs = coeffs
    cal.force_est = force_est
    cal.cal_type = cal_type
    cal.r_squared = r_squared
    cal.bias = bias
    return cal


def get_distance_values(cal: BalanceCalibration) -> Dict[str, float]:
    distances = cal.distances.values
    result = {'dx1': 0.0, 'dx2': 0.0, 'dy1': 0.0, 'dy2': 0.0}
    search_terms = {'dx1': ['x1', 'N1'], 'dx2': ['x2', 'N2'],
                    'dy1': ['y1', 'Y1'], 'dy2': ['y2', 'Y2']}
    for key, terms in search_terms.items():
        for dist_name, dist_value in distances.items():
            for term in terms:
                if term in dist_name:
                    result[key] = dist_value
                    break
    return result


def calc_brf_forces(raw_data: Dict[str, np.ndarray],
                    cal: BalanceCalibration,
                    geo: Optional[Geometry] = None,
                    balance_config: str = 'Force') -> BRFForces:
    """Bridge volts → element loads → body-frame forces/moments.

    ``raw_data`` holds per-channel voltage arrays keyed N1,N2,Y1,Y2,Axial,
    Roll (or AftPitch/AftYaw/FwdPitch/FwdYaw for moment balances) plus
    optional ``Excitation`` (volts) for normalization.
    """
    geo = geo or Geometry()
    brf = BRFForces()

    dist = get_distance_values(cal)
    dx1, dx2 = dist['dx1'], dist['dx2']
    dy1, dy2 = dist['dy1'], dist['dy2']

    if 'Excitation' in raw_data:
        excitation = np.asarray(raw_data['Excitation'], dtype=float)
        excitation = np.where(np.abs(excitation) < 1e-6, 1.0, excitation)
    else:
        excitation = np.ones(len(next(iter(raw_data.values()))))

    if balance_config == 'Force':
        names = ['N1', 'N2', 'Y1', 'Y2', 'Axial', 'Roll'] \
            if 'N1' in raw_data else \
            ['AftPitch', 'AftYaw', 'FwdPitch', 'FwdYaw', 'Axial', 'Roll']
        raw_volts = np.column_stack(
            [np.asarray(raw_data[n], dtype=float) / excitation
             for n in names])
    else:
        if 'AftPitch' in raw_data:
            ch = ['AftPitch', 'AftYaw', 'FwdPitch', 'FwdYaw']
        else:
            ch = ['N1', 'N2', 'Y1', 'Y2']
        raw_volts = np.column_stack(
            [np.asarray(raw_data[n], dtype=float) / excitation
             for n in ch + ['Axial', 'Roll']])
        if dx1 > 2:
            dx1 /= 2
            dx2 /= 2
            dy1 /= 2
            dy2 /= 2

    order_map = {'Linear': 1, 'Quadratic': 2, 'Cubic': 3}
    order = order_map.get(cal.cal_type, 1)
    X = form_higher_order_terms(raw_volts, order)
    elements = X @ cal.coeffs
    brf.elements = elements

    mshift = geo.mshift

    if balance_config == 'Force':
        brf.Fz = elements[:, 0] + elements[:, 1]
        brf.Fy = elements[:, 2] + elements[:, 3]
        brf.Fx = elements[:, 4]
        brf.Mx = elements[:, 5] - brf.Fy * mshift[2]
        brf.My = (elements[:, 0] * (dx1 - mshift[0]) -
                  elements[:, 1] * (dx2 + mshift[0]) -
                  brf.Fx * mshift[2])
        brf.Mz = (elements[:, 2] * (dy1 + mshift[1]) -
                  elements[:, 3] * (dy2 - mshift[1]) -
                  brf.Fy * mshift[0])
    else:
        brf.Fz = (elements[:, 0] - elements[:, 2]) / (dx1 + dx2)
        brf.Fy = (elements[:, 1] - elements[:, 3]) / (dy1 + dy2)
        brf.Fx = elements[:, 4]
        brf.Mx = elements[:, 5] - brf.Fy * mshift[2]
        brf.My = ((elements[:, 0] + elements[:, 2]) / 2 -
                  brf.Fx * mshift[2] - brf.Fz * mshift[0])
        brf.Mz = ((elements[:, 1] + elements[:, 3]) / 2 +
                  brf.Fx * mshift[1] + brf.Fy * mshift[0])

    return brf


# ─────────────────────────────────────────────────────────────────────────
#  Load-limit monitoring (this package's addition)
# ─────────────────────────────────────────────────────────────────────────

def element_utilization(cal: BalanceCalibration,
                        elements: np.ndarray) -> Dict[str, float]:
    """Peak |element load| as a fraction of the balance's rated maximum.

    Returns {channel: utilization} with 1.0 = at the rated limit. Channels
    without a matching max-load entry are omitted.
    """
    out: Dict[str, float] = {}
    if elements.size == 0:
        return out
    peaks = np.max(np.abs(np.atleast_2d(elements)), axis=0)
    for i, name in enumerate(cal.force_channels[:6]):
        limit = cal.max_loads.values.get(name)
        if limit is None:       # tolerant match (e.g. 'Ax' vs 'Axial')
            for k, v in cal.max_loads.values.items():
                if k.lower().startswith(name[:2].lower()) or \
                        name.lower().startswith(k[:2].lower()):
                    limit = v
                    break
        if limit and limit > 0 and i < peaks.size:
            out[name] = float(peaks[i] / limit)
    return out


def balance_summary(cal: BalanceCalibration) -> str:
    """One-line description for display / dataset metadata."""
    d = cal.description
    r2 = (f", R² min {cal.r_squared.min():.5f}"
          if cal.r_squared.size else "")
    return (f"{d.balance_type or 'balance'} SN {d.serial_number or '?'} "
            f"({cal.cal_type}{r2})")
