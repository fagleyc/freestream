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
class ChannelAnomaly:
    """One suspect voltage VALUE: channel c of point (key, index)
    deviates from its section's robust volts-vs-load trend. These are
    the off-diagonal (cross-talk) corruptions: the point's own element
    can fit perfectly while a foreign channel's bad value bends the
    cross-talk coefficients — and through them, other elements'
    slopes. Repairable in place (``expected_v``) without losing the
    row."""
    key: str
    index: int               # row within the orientation
    channel: int             # bridge channel column 0-5
    channel_name: str
    measured_v: float        # raw volts as stored
    expected_v: float        # robust section-trend prediction
    z: float

    @property
    def deviation_v(self) -> float:
        return self.measured_v - self.expected_v


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
    channel_anomalies: List[ChannelAnomaly] = field(default_factory=list)
    crosstalk_notes: List[str] = field(default_factory=list)
    r_squared_repaired: Optional[np.ndarray] = None
    #: slope of predicted-vs-applied within each element's OWN sections
    #: (1.0 = perfect; "points look linear but slope way off" shows here)
    own_slopes: Optional[np.ndarray] = None
    #: leave-section-out influence findings + collinearity notes
    influence_notes: List[str] = field(default_factory=list)

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


def channel_trend(session: CalSession, key: str, channel: int
                  ) -> Tuple[np.ndarray, np.ndarray, np.ndarray,
                             np.ndarray]:
    """Robust linear trend of one channel's RAW volts vs load within
    one section. Returns (loads, volts, trend, robust_z) over the
    section's active points (order = active order). Two rounds of MAD
    rejection keep the trend from being dragged by the anomaly itself.
    """
    pts = session.active_points(key)
    loads = np.array([p.load for p in pts])
    volts = np.array([p.volts[channel] for p in pts])
    n = len(pts)
    if n < 4 or np.ptp(loads) == 0:
        z = np.zeros_like(volts)
        return loads, volts, np.full_like(volts, np.median(volts)), z

    # Theil-Sen line: median of pairwise slopes + median intercept.
    # A single gross value cannot drag it, even at a high-leverage
    # load (an ordinary fit passes through an apex outlier and hides
    # it; LOO fails more subtly — the glitch contaminates every OTHER
    # point's training set and inflates the scale).
    slopes = []
    for i in range(n):
        dx = loads - loads[i]
        m = dx != 0
        slopes.extend(((volts[m] - volts[i]) / dx[m]).tolist())
    slope = float(np.median(slopes)) if slopes else 0.0
    intercept = float(np.median(volts - slope * loads))
    trend = slope * loads + intercept
    resid = volts - trend

    # jackknifed robust z: score each residual against the scale of
    # the OTHERS, so a lone gross value on an otherwise exact section
    # (scale → 0) still scores huge instead of self-masking
    z = np.zeros(n)
    for i in range(n):
        others = np.delete(resid, i)
        med = float(np.median(others))
        mad = float(np.median(np.abs(others - med)))
        scale = max(mad / 0.6745, 1e-12)
        z[i] = min(max((resid[i] - med) / scale, -1e6), 1e6)
    return loads, volts, trend, z


def _scan_channels(session: CalSession, diag: "FitDiagnostics",
                   outlier_z: float) -> None:
    """Off-diagonal value scan + cross-talk burden analysis."""
    channels = [el.channel for el in session.elements]
    # per-channel span within each section, and each channel's span in
    # its OWN calibration sections (the signal the fit must resolve)
    spans: Dict[Tuple[str, int], float] = {}
    own_span = np.zeros(len(channels))
    for orient in session.orientations:
        pts = session.active_points(orient.key)
        if len(pts) < 2:
            continue
        arr = np.array([p.volts for p in pts])
        col = next(i for i, el in enumerate(session.elements)
                   if el.name == orient.element.name)
        for c in range(len(channels)):
            spans[(orient.key, c)] = float(np.ptp(arr[:, c]))
        own_span[col] = max(own_span[col], float(np.ptp(arr[:, col])))

    for orient in session.orientations:
        pts = session.active_points(orient.key)
        if len(pts) < 4:
            continue
        col = next(i for i, el in enumerate(session.elements)
                   if el.name == orient.element.name)
        idx_map = [i for i, p in enumerate(
            session.points.get(orient.key, [])) if not p.excluded]
        for c in range(len(channels)):
            span = spans.get((orient.key, c), 0.0)
            # discrete value anomalies vs the robust section trend
            loads, volts, trend, z = channel_trend(session, orient.key,
                                                  c)
            for r in range(len(volts)):
                dev = volts[r] - trend[r]
                if (abs(z[r]) > outlier_z
                        and abs(dev) > max(0.15 * span, 2e-5)):
                    diag.channel_anomalies.append(ChannelAnomaly(
                        key=orient.key, index=idx_map[r], channel=c,
                        channel_name=channels[c],
                        measured_v=float(volts[r]),
                        expected_v=float(trend[r]), z=float(z[r])))
            # cross-talk burden: a foreign section driving this channel
            # harder than its own calibration did dominates its
            # coefficients — the classic good-points-wrong-slope cause
            if (c != col and own_span[c] > 0
                    and span > 0.5 * own_span[c]):
                diag.crosstalk_notes.append(
                    f"[{orient.section}] drives {channels[c]} through "
                    f"{span * 1e3:.2f} mV — "
                    f"{span / own_span[c]:.1f}x that channel's span in "
                    f"its own calibration sections; these off-diagonal "
                    f"rows steer the {channels[c]} coefficients (and "
                    f"its element's slope)")


def _repaired_preview(session: CalSession,
                      diag: "FitDiagnostics") -> None:
    """R^2 if every flagged channel VALUE were repaired to its trend."""
    if not diag.channel_anomalies:
        return
    import copy
    trial = copy.deepcopy(session)
    for a in diag.channel_anomalies:
        p = trial.points[a.key][a.index]
        p.volts = list(p.volts)
        p.volts[a.channel] = a.expected_v
    try:
        force, volts, _rows, _w, _sz = _assemble(trial)
        X = _poly(volts, _ORDER.get(diag.cal_type, 1))
        ck, *_r = np.linalg.lstsq(X, force, rcond=None)
        diag.r_squared_repaired = _r_squared(force, X @ ck)
    except Exception:                       # noqa: BLE001
        diag.r_squared_repaired = None


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

    # off-diagonal (cross-talk) value scan + repaired preview
    _scan_channels(session, diag, outlier_z)
    _repaired_preview(session, diag)
    _influence_scan(diag, force, volts, X, rows)
    return diag


def _influence_scan(diag: FitDiagnostics, force, volts, X, rows) -> None:
    """Own-section slopes, bridge collinearity, and leave-section-out
    influence — the diagnosis for 'points look linear but the slope is
    way off with no outliers': collinear bridge columns carrying
    contradictory targets between section groups (seen 2026-07-22 in
    50lbCalV6: the Mx runs bent the fwd section for real, forcing the
    fwd coefficients into a giant canceling pair)."""
    n_el = force.shape[1]
    keys = np.array([r[0] for r in rows])
    coeffs, *_r = np.linalg.lstsq(X, force, rcond=None)
    est = X @ coeffs

    # own-section slope of predicted vs applied
    slopes = np.full(n_el, np.nan)
    own_masks = []
    for j, name in enumerate(diag.channels):
        own = np.array([k.rsplit("_", 1)[0] == name for k in keys])
        own_masks.append(own)
        if own.sum() >= 3 and np.ptp(force[own, j]) > 0:
            slopes[j] = float(np.polyfit(force[own, j],
                                         est[own, j], 1)[0])
    diag.own_slopes = slopes

    # near-collinear bridge columns (the enabler)
    for a in range(n_el):
        for b in range(a + 1, n_el):
            if np.ptp(volts[:, a]) < 1e-9 or np.ptp(volts[:, b]) < 1e-9:
                continue
            c = float(np.corrcoef(volts[:, a], volts[:, b])[0, 1])
            if abs(c) > 0.95:
                diag.influence_notes.append(
                    f"bridge columns {diag.channels[a]} and "
                    f"{diag.channels[b]} are near-collinear over the "
                    f"whole dataset (corr {c:+.3f}) — the fit cannot "
                    f"separate them, so contradictory targets between "
                    f"section groups corrupt both coefficients")

    # leave-section-group-out: which group's rows break which element
    groups = sorted({k.rsplit("_", 1)[0] for k in keys})
    for j, name in enumerate(diag.channels):
        if not np.isfinite(slopes[j]) or abs(slopes[j] - 1.0) < 0.05:
            continue
        own = own_masks[j]
        best = None
        for g in groups:
            if g == name:
                continue
            m = ~np.array([k.rsplit("_", 1)[0] == g for k in keys])
            cg, *_x = np.linalg.lstsq(X[m], force[m], rcond=None)
            e = (X @ cg)[own, j]
            a = force[own, j]
            sl = float(np.polyfit(a, e, 1)[0])
            if best is None or abs(sl - 1.0) < abs(best[1] - 1.0):
                best = (g, sl)
        if best and abs(best[1] - 1.0) < 0.5 * abs(slopes[j] - 1.0):
            diag.influence_notes.append(
                f"{name}: own-section slope {slopes[j]:.3f} recovers "
                f"to {best[1]:.3f} when the [{best[0]}] section rows "
                f"are left out of the fit — those runs applied a REAL "
                f"unrecorded load on this element's bridges "
                f"(re-rig and re-acquire that section; value repairs "
                f"cannot fix this)")


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
    if diag.own_slopes is not None:
        lines.append("")
        lines.append("Own-section slope (predicted vs applied; 1.0 = "
                     "perfect):")
        lines.append("  " + "  ".join(
            f"{n} {s:.3f}" for n, s in zip(diag.channels,
                                           diag.own_slopes)
            if np.isfinite(s)))
    if diag.influence_notes:
        lines.append("")
        lines.append("Fit-structure findings (collinearity / section "
                     "influence):")
        for w in diag.influence_notes:
            lines.append(f"  * {w}")
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
    if diag.channel_anomalies:
        lines.append("")
        lines.append("Off-diagonal channel-value anomalies (suspect "
                     "single VOLTAGES, repairable in place):")
        lines.append(f"  {'section':<14s}{'row':>4s}{'channel':>10s}"
                     f"{'measured':>12s}{'trend':>12s}{'dev':>10s}"
                     f"{'z':>7s}")
        for a in sorted(diag.channel_anomalies,
                        key=lambda x: -abs(x.z))[:max_outliers]:
            lines.append(f"  {a.key:<14s}{a.index + 1:>4d}"
                         f"{a.channel_name:>10s}"
                         f"{a.measured_v:>12.6f}{a.expected_v:>12.6f}"
                         f"{a.deviation_v:>+10.2e}{a.z:>7.1f}")
        if diag.r_squared_repaired is not None:
            lines.append("")
            lines.append(f"  {'Element':<10s}{'R^2 (as-is)':>13s}"
                         f"{'R^2 (values repaired)':>23s}")
            for i, name in enumerate(diag.channels):
                lines.append(
                    f"  {name:<10s}{diag.r_squared[i]:>13.6f}"
                    f"{diag.r_squared_repaired[i]:>23.6f}")
    if diag.crosstalk_notes:
        lines.append("")
        lines.append("Cross-talk burden (structural, NOT single "
                     "points):")
        for w in diag.crosstalk_notes:
            lines.append(f"  * {w}")
    if diag.warnings:
        lines.append("")
        lines.append("Warnings:")
        for w in diag.warnings:
            lines.append(f"  * {w}")
    lines.append("")
    return "\n".join(lines)
