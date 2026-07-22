"""Calibration fit diagnostics: outlier detection and per-section
health checks, with every point traceable back to its orientation/row.

Reproduces the consumers' data assembly (``balcal.read_vol_file``)
in-memory from the live session — per-section zero offset from the
load==0 rows, per-row excitation normalization, |load| x section-sign
folding — then fits the same least-squares matrix. Because the row
map is kept, residuals map back to clickable, deletable points.

Why good slopes but terrible R^2 happens (what these diagnostics
separate):

* a few gross points (weight misread, swinging average) → large
  |robust z| on individual residuals → *outliers*;
* a section with NO zero-load rows → its offset cannot be removed and
  every point in it is biased by the same amount → large per-section
  mean residual with small scatter → *section offset*, fix by adding
  0-load points (or excluding the section), not by deleting points;
* excitation anomalies → flagged when a row's excitation strays from
  the session median.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import numpy as np

from .session import CalSession

#: |robust z| above this marks a point as an outlier
OUTLIER_Z = 3.5
#: a section whose |mean residual| exceeds this multiple of the global
#: robust residual scale is flagged as offset-shifted
SECTION_OFFSET_FACTOR = 3.0

_ORDER = {"Linear": 1, "Quadratic": 2, "Cubic": 3}


@dataclass
class PointDiag:
    """One fitted point, mapped back to the session."""
    key: str                 # orientation key ("N1_pos")
    index: int               # row within that orientation (incl. excluded)
    element: int             # element column this section loads
    load: float              # signed applied load
    predicted: float         # fitted prediction for that element
    residual: float          # predicted - load
    zscore: float            # robust z of the residual (per element)
    excitation: float
    is_outlier: bool = False


@dataclass
class SectionDiag:
    key: str
    n_points: int
    has_zero: bool           # any load==0 rows (offset removal possible)
    mean_residual: float
    std_residual: float
    offset_suspect: bool     # biased as a block (see module docstring)


@dataclass
class FitDiagnostics:
    cal_type: str
    channels: List[str]
    coeffs: np.ndarray
    r_squared: np.ndarray            # per element, all active points
    r_squared_clean: np.ndarray      # per element, outliers removed
    points: List[PointDiag] = field(default_factory=list)
    sections: List[SectionDiag] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def outliers(self) -> List[PointDiag]:
        return [p for p in self.points if p.is_outlier]


def _assemble(session: CalSession):
    """Session → (force, volts, rows) exactly as read_vol_file does.

    ``rows[i] = (key, local_index, element_col)`` maps matrix row i
    back to the session point (local_index counts ALL points of the
    orientation, so it stays valid for delete/exclude operations).
    """
    force_rows: List[np.ndarray] = []
    volt_rows: List[np.ndarray] = []
    rows: List[Tuple[str, int, int]] = []
    warnings: List[str] = []
    sections_zero: Dict[str, bool] = {}

    elements = session.elements
    for orient in session.orientations:
        pts = [(i, p) for i, p in enumerate(session.points.get(
            orient.key, [])) if not p.excluded]
        if not pts:
            continue
        col = next(i for i, el in enumerate(elements)
                   if el.name == orient.element.name)
        data = np.array([[p.load, *p.volts, p.excitation]
                         for _i, p in pts])
        zero_rows = np.where(data[:, 0] == 0)[0]
        sections_zero[orient.key] = bool(len(zero_rows))
        if len(zero_rows):
            zero_offset = np.mean(data[zero_rows, 1:7], axis=0)
        else:
            zero_offset = np.zeros(6)
            warnings.append(
                f"[{orient.section}] has no 0-load points — the zero "
                f"offset cannot be removed; every point in this "
                f"section is biased (this alone wrecks R^2 while "
                f"leaving slopes intact)")
        exc = data[:, 7:8]
        volts = (data[:, 1:7] - zero_offset) / exc
        # parser folds |load| x section sign
        loads = np.abs(data[:, 0]) * orient.sign
        for r, (i, _p) in enumerate(pts):
            f = np.zeros(len(elements))
            f[col] = loads[r]
            force_rows.append(f)
            volt_rows.append(volts[r])
            rows.append((orient.key, i, col))
    if not force_rows:
        raise ValueError("No active test points to fit")
    return (np.array(force_rows), np.array(volt_rows), rows, warnings,
            sections_zero)


def _poly(v: np.ndarray, order: int) -> np.ndarray:
    if order == 1:
        return v
    if order == 2:
        return np.hstack([v, v * v])
    return np.hstack([v, v * v, v * v * v])


def _r_squared(force, force_est) -> np.ndarray:
    ss_res = np.sum((force - force_est) ** 2, axis=0)
    ss_tot = np.sum((force - np.mean(force, axis=0)) ** 2, axis=0)
    with np.errstate(divide="ignore", invalid="ignore"):
        return 1 - ss_res / ss_tot


def _robust_z(x: np.ndarray,
              ref: Optional[np.ndarray] = None) -> np.ndarray:
    """MAD-based z-scores of ``x``, scaled by the reference sample
    (defaults to ``x`` itself). Passing the currently-kept residuals as
    ``ref`` lets flagged points be scored against the clean scale."""
    r = x if ref is None else ref
    med = np.median(r)
    mad = np.median(np.abs(r - med))
    if mad < 1e-15:
        std = np.std(r)
        return ((x - med) / std if std > 1e-15
                else np.zeros_like(x))
    return 0.6745 * (x - med) / mad


def diagnose(session: CalSession, cal_type: str = "Linear",
             outlier_z: float = OUTLIER_Z) -> FitDiagnostics:
    """Fit the active points and analyse residuals per point/section."""
    force, volts, rows, warnings, sections_zero = _assemble(session)
    order = _ORDER.get(cal_type, 1)
    X = _poly(volts, order)
    coeffs, *_rest = np.linalg.lstsq(X, force, rcond=None)
    force_est = X @ coeffs
    r2 = _r_squared(force, force_est)

    channels = [el.name for el in session.elements]
    diag = FitDiagnostics(cal_type=cal_type, channels=channels,
                          coeffs=coeffs, r_squared=r2,
                          r_squared_clean=r2.copy(),
                          warnings=list(warnings))

    # Iterative outlier detection (sequential MAD rejection): flagged
    # points are dropped, the fit repeats on the survivors, and ALL
    # points are re-scored against the clean fit's residual scale.
    # This defeats masking — two gross points inflate the MAD enough
    # to hide each other from a single pass. Residuals are on each
    # point's OWN element column.
    n = len(rows)
    cols = np.array([r[2] for r in rows])
    min_rows = X.shape[1] + 1
    flagged = np.zeros(n, dtype=bool)
    resid = np.array([force_est[i, cols[i]] - force[i, cols[i]]
                      for i in range(n)])
    zscores = np.zeros(n)
    signif = np.zeros(n, dtype=bool)
    for _iteration in range(5):
        keep = ~flagged
        if keep.sum() < min_rows:
            break
        ck, *_r = np.linalg.lstsq(X[keep], force[keep], rcond=None)
        est = X @ ck
        resid = np.array([est[i, cols[i]] - force[i, cols[i]]
                          for i in range(n)])
        zscores = np.zeros(n)
        signif = np.zeros(n, dtype=bool)
        for col in range(len(channels)):
            idx = np.where(cols == col)[0]
            kidx = np.where((cols == col) & keep)[0]
            if len(kidx) >= 4:
                zscores[idx] = _robust_z(resid[idx], ref=resid[kidx])
                # a z spike on numerically-perfect data is noise, not
                # an outlier: the residual must also be significant
                # relative to the element's load scale
                scale = max(float(np.max(np.abs(force[idx, col]))),
                            1e-12)
                signif[idx] = np.abs(resid[idx]) > 1e-6 * scale
        new_flagged = (np.abs(zscores) > outlier_z) & signif
        if np.array_equal(new_flagged, flagged):
            break
        flagged = new_flagged

    exc_all = np.array([session.points[k][j].excitation
                        for k, j, _c in rows])
    exc_med = float(np.median(exc_all))

    # report predictions/residuals from the final CLEAN fit — outliers
    # then stand far off the y = x line instead of dragging it along
    for i, (key, j, col) in enumerate(rows):
        p = session.points[key][j]
        diag.points.append(PointDiag(
            key=key, index=j, element=col,
            load=float(force[i, col]),
            predicted=float(est[i, col]),
            residual=float(resid[i]), zscore=float(zscores[i]),
            excitation=p.excitation,
            is_outlier=bool(flagged[i])))
        if exc_med and abs(p.excitation - exc_med) > 0.05 * abs(exc_med):
            diag.warnings.append(
                f"[{key}] row {j + 1}: excitation "
                f"{p.excitation:.3f} V is >5% from the session median "
                f"{exc_med:.3f} V")

    # per-section health
    scale = float(np.median(np.abs(resid - np.median(resid)))) / 0.6745 \
        if len(resid) > 3 else float(np.std(resid))
    for orient in session.orientations:
        pr = [p for p in diag.points if p.key == orient.key]
        if not pr:
            continue
        res = np.array([p.residual for p in pr])
        mean_r, std_r = float(np.mean(res)), float(np.std(res))
        offset_suspect = (
            len(res) >= 3 and scale > 0
            and abs(mean_r) > SECTION_OFFSET_FACTOR * scale
            and std_r < abs(mean_r))
        diag.sections.append(SectionDiag(
            key=orient.key, n_points=len(pr),
            has_zero=sections_zero.get(orient.key, False),
            mean_residual=mean_r, std_residual=std_r,
            offset_suspect=offset_suspect))
        if offset_suspect:
            diag.warnings.append(
                f"[{orient.section}] looks offset-shifted as a block "
                f"(mean residual {mean_r:+.3g}, scatter {std_r:.3g}) — "
                f"suspect the zero reference for this section, not "
                f"individual points")

    # R^2 with outliers removed (preview of the achievable fit)
    keep = np.array([not p.is_outlier for p in diag.points])
    if (~keep).any() and keep.sum() > len(channels) * order + 1:
        Xk, fk = X[keep], force[keep]
        ck, *_r = np.linalg.lstsq(Xk, fk, rcond=None)
        diag.r_squared_clean = _r_squared(fk, Xk @ ck)
    return diag


def diagnostics_text(diag: FitDiagnostics, max_outliers: int = 15) -> str:
    """Plain-text diagnostics block appended to the cal report."""
    lines = ["Fit diagnostics", "-" * 60]
    out = sorted(diag.outliers(), key=lambda p: -abs(p.zscore))
    if out:
        lines.append(f"{len(out)} outlier(s), |robust z| > "
                     f"{OUTLIER_Z:g} (worst first):")
        lines.append(f"  {'orientation':<14s}{'row':>4s}{'applied':>10s}"
                     f"{'predicted':>11s}{'residual':>10s}{'z':>7s}")
        for p in out[:max_outliers]:
            lines.append(f"  {p.key:<14s}{p.index + 1:>4d}"
                         f"{p.load:>10.3f}{p.predicted:>11.4f}"
                         f"{p.residual:>+10.4f}{p.zscore:>7.1f}")
        if len(out) > max_outliers:
            lines.append(f"  … and {len(out) - max_outliers} more")
        lines.append("")
        lines.append(f"  {'Element':<10s}{'R^2 (all)':>12s}"
                     f"{'R^2 (no outliers)':>19s}")
        for i, name in enumerate(diag.channels):
            lines.append(f"  {name:<10s}{diag.r_squared[i]:>12.6f}"
                         f"{diag.r_squared_clean[i]:>19.6f}")
    else:
        lines.append("No outliers flagged "
                     f"(|robust z| <= {OUTLIER_Z:g}).")
    lines.append("")
    lines.append("Per-section health:")
    lines.append(f"  {'section':<14s}{'pts':>4s}{'0-load':>7s}"
                 f"{'mean resid':>12s}{'scatter':>10s}")
    for s in diag.sections:
        flag = "  ← OFFSET?" if s.offset_suspect else ""
        zero = "yes" if s.has_zero else "NO"
        lines.append(f"  {s.key:<14s}{s.n_points:>4d}{zero:>7s}"
                     f"{s.mean_residual:>+12.4g}"
                     f"{s.std_residual:>10.4g}{flag}")
    if diag.warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in diag.warnings:
            lines.append(f"  * {w}")
    lines.append("")
    return "\n".join(lines)
