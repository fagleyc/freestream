"""Freestream sweep-cell grammar — the ONE canonical axis-cell parser.

A *sweep cell* is a single alpha / beta / Mach entry on the Run Matrix of a
Freestream run-sheet workbook (see :mod:`freestream.runbook`).  This module is
the single source of truth for how such a cell expands into an explicit list of
setpoints.  It is pure Python (stdlib + optional caller-supplied loaders); it
imports nothing from Qt and nothing from the rest of Freestream, so both the
headless engine and the GUI share exactly one grammar.

Grammar (matches the workbook ``Guide`` tab and Casey's reference parser)::

    (blank)          not swept                         ""        -> []
    single value     one point                         "2"       -> [2]
    comma list       explicit points                   "0,2,4"   -> [0, 2, 4]
    range  a:d:b     START : DELTA : END, end-inclusive "-4:2:8"  -> [-4,-2,..,8]
    return …R        append the reverse leg (up/dn)     "0:2:10R" -> up 0..10, dn 8..0
    mix              comma-join any of the above        "-4:2:8, 10, 12"
    named  @name     a row on the Named Arrays tab      "@alpha_fine"
    file   csv:name  one column from an external CSV    "csv:aoa.csv"

Key rules:

* The **middle** number of a range is the DELTA (step), not the end — this is
  MATLAB-style ``start:step:end`` colon notation.  This REPLACES the old
  ``freestream.runsheet`` ``start:stop:step`` reading.
* If the delta does not land on the end exactly, the last partial point is
  dropped: ``0:2:9`` -> ``[0, 2, 4, 6, 8]``.
* ``…R`` (return sweep) mirrors the leg back to its start, **omitting** the
  duplicated apex, and tags the forward values ``"up"`` and the reverse values
  ``"dn"`` so the two hysteresis legs never collide.
* Mach only: with ``ensure_zero_for_mach=True`` a ``0`` (air-off / wind-off)
  point is auto-prepended to a non-empty array unless a ``0`` is already there.

Two views of the same expansion:

* :func:`expand` returns a flat list of numbers (values only).
* :func:`expand_tagged` returns a list of ``(value, leg)`` pairs, where ``leg``
  is ``"up"`` / ``"dn"`` on a return-sweep axis and ``None`` otherwise.

:func:`build_points` composes three cells into the nested aero matrix
(Mach outermost, air-off first → beta → alpha innermost), carrying the leg tag
of the innermost swept axis onto each point.
"""

from __future__ import annotations

import re
from typing import Callable, List, Optional, Sequence, Tuple, Union

__all__ = [
    "expand",
    "expand_tagged",
    "build_points",
    "Number",
    "Leg",
    "TaggedValue",
    "GrammarError",
]

Number = Union[int, float]
Leg = Optional[str]                      # "up" | "dn" | None
TaggedValue = Tuple[Number, Leg]

#: CSV loader signature: name -> list of numbers (one column).
CsvLoader = Callable[[str], Sequence[Number]]

_RANGE_RE = re.compile(
    r"^(-?\d+\.?\d*)\s*:\s*(-?\d+\.?\d*)\s*:\s*(-?\d+\.?\d*)$")
_EPS = 1e-9


class GrammarError(ValueError):
    """Raised when a cell cannot be parsed under the sweep grammar."""


def _tidy(value: float) -> Number:
    """Kill float noise and collapse integer-valued floats to ``int``."""
    value = round(value, 6)
    if value == 0.0:
        value = 0.0                      # squash -0.0
    return int(value) if float(value).is_integer() else value


def _expand_range(start: float, delta: float, end: float) -> List[Number]:
    """START:DELTA:END inclusive of END within tolerance; partial tail dropped.

    A zero delta degenerates to the single start point (never an infinite
    walk).  The delta's magnitude is used; its sign is taken from the
    start→end direction so ``4:2:-4`` and ``4:-2:-4`` both descend.
    """
    if delta == 0.0:
        return [_tidy(start)]
    step = abs(delta) if end >= start else -abs(delta)
    seq: List[Number] = []
    value = start
    if end >= start:
        while value <= end + _EPS:
            seq.append(_tidy(value))
            value = round(value + step, 6)
    else:
        while value >= end - _EPS:
            seq.append(_tidy(value))
            value = round(value + step, 6)
    return seq


def _expand_token(token: str, named: dict,
                  csv_loader: Optional[CsvLoader],
                  _depth: int = 0) -> List[TaggedValue]:
    """Expand ONE comma-delimited token into ``(value, leg)`` pairs."""
    t = token.strip()
    if not t:
        return []
    if _depth > 20:
        raise GrammarError(f"named-array reference nested too deeply "
                           f"near {token!r}")

    # @name — a reusable definition from the Named Arrays tab
    if t.startswith("@"):
        name = t[1:].strip()
        if name not in named:
            raise GrammarError(f"unknown named array @{name}")
        # a named definition is itself a full cell (may contain commas/ranges)
        return _expand_cell(str(named[name]), named, csv_loader, _depth + 1)

    # csv:file — one column from an external CSV (loader supplied by caller)
    if t.lower().startswith("csv:"):
        ref = t[4:].strip()
        if csv_loader is None:
            raise GrammarError(
                f"cell {t!r} needs a CSV loader but none was supplied")
        return [(_coerce_number(v), None) for v in csv_loader(ref)]

    # range with optional trailing return flag
    ret = t.endswith(("R", "r"))
    core = t[:-1].strip() if ret else t
    m = _RANGE_RE.match(core)
    if m:
        start, delta, end = (float(m.group(1)), float(m.group(2)),
                             float(m.group(3)))
        forward = _expand_range(start, delta, end)
        if ret and len(forward) > 1:
            reverse = forward[-2::-1]        # mirror, drop the duplicated apex
            return ([(v, "up") for v in forward]
                    + [(v, "dn") for v in reverse])
        return [(v, None) for v in forward]

    # single number
    return [(_coerce_number(core), None)]


def _coerce_number(text) -> Number:
    try:
        value = float(str(text).strip())
    except (TypeError, ValueError) as exc:
        raise GrammarError(f"not a number: {text!r}") from exc
    return _tidy(value)


def _expand_cell(cell, named: dict, csv_loader: Optional[CsvLoader],
                 _depth: int = 0) -> List[TaggedValue]:
    text = "" if cell is None else str(cell).strip()
    if text == "" or text.lower() == "none":
        return []
    out: List[TaggedValue] = []
    for token in text.split(","):
        out.extend(_expand_token(token, named, csv_loader, _depth))
    return out


def expand_tagged(cell, named: Optional[dict] = None,
                  csv_loader: Optional[CsvLoader] = None,
                  ensure_zero_for_mach: bool = False) -> List[TaggedValue]:
    """Expand a cell into ``[(value, leg)]`` pairs (leg = up/dn/None).

    See the module docstring for the grammar.  With
    ``ensure_zero_for_mach`` a leading ``(0, None)`` air-off point is
    prepended to a non-empty result that does not already contain ``0``.
    """
    named = named or {}
    out = _expand_cell(cell, named, csv_loader)
    if ensure_zero_for_mach and out and not any(v == 0 for v, _ in out):
        out = [(0, None)] + out
    return out


def expand(cell, named: Optional[dict] = None,
           csv_loader: Optional[CsvLoader] = None,
           ensure_zero_for_mach: bool = False) -> List[Number]:
    """Expand a cell into a flat list of numeric setpoints (legs dropped)."""
    return [v for v, _ in expand_tagged(
        cell, named, csv_loader, ensure_zero_for_mach)]


def build_points(alpha_cell, beta_cell, mach_cell,
                 named: Optional[dict] = None,
                 csv_loader: Optional[CsvLoader] = None) -> List[dict]:
    """Compose three cells into the nested aero matrix.

    Nesting is fixed: **Mach outermost (air-off 0 first) → beta → alpha
    innermost/fastest**.  Each returned dict is
    ``{"mach": m, "beta": b, "alpha": a, "leg": leg}`` where ``leg`` is the
    return-sweep tag of the innermost swept axis that carries one
    (alpha, then beta, then Mach), else ``None``.  An omitted axis (blank
    cell) contributes a single ``None`` placeholder so the product still
    yields points.
    """
    named = named or {}
    a_vals = expand_tagged(alpha_cell, named, csv_loader) or [(None, None)]
    b_vals = expand_tagged(beta_cell, named, csv_loader) or [(None, None)]
    m_vals = (expand_tagged(mach_cell, named, csv_loader,
                            ensure_zero_for_mach=True) or [(0, None)])

    points: List[dict] = []
    for mach, m_leg in m_vals:
        for beta, b_leg in b_vals:
            for alpha, a_leg in a_vals:
                leg = a_leg or b_leg or m_leg
                points.append({"mach": mach, "beta": beta,
                               "alpha": alpha, "leg": leg})
    return points
