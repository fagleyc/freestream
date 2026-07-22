"""Writer for "Voltage Calibration File 3.1" (.vol).

Reproduces the format of ``balance_cal/2025_06_06_2 100 lb.vol`` exactly:
``-->`` key/value header, ``[Balance Description]`` / ``[Maximal Balance
Loads]`` / ``[Distances]`` blocks, then one ``[<element> pos|neg]``
section per load orientation with a ``Number of Loads-->`` count, a
column-header line and comma-separated data rows (load, six bridge
volts, excitation volts). Output is round-trip compatible with the
device drivers' ``balcal.read_vol_file``.
"""

from __future__ import annotations

import re
from datetime import date
from typing import List

from .session import (BalanceKind, CalSession, TestPoint,
                      elements_for, volt_columns_for)

FORMAT_LINE = "Voltage Calibration File 3.1"


def _num(v: float) -> str:
    """Volt columns: 10-significant-digit scientific, E-notation."""
    return f"{v:.10E}"


def _load(v: float) -> str:
    """Load column: plain notation, no trailing zeros (0, 5, -15, 6.41).
    10 significant digits so fractional moment-arm products survive the
    round trip."""
    if v == int(v):
        return str(int(v))
    return f"{v:.10g}"


def _header_lines(session: CalSession) -> List[str]:
    d = session.cal_date
    lines = [
        FORMAT_LINE,
        f"Date--> {d.month}/{d.day}/{d.year}",
        f"Calibration performed by--> {session.operator}",
        "",
        "[Balance Description]",
        f"Balance Type--> {session.kind.type_string}",
        f"Balance Serial Number--> {session.serial_number}",
        f"Balance outer Diameter--> {session.outer_diameter}",
        "",
        "[Maximal Balance Loads]",
    ]
    for el in session.elements:
        val = session.max_loads.get(el.name)
        if val is None:
            # A valueless line ("N1-->  lb") would make the whole file
            # unparseable — read_vol_file does float(value.split()[0]).
            # The parser tolerates a missing key, so omit the line;
            # validate_session() lets callers warn the operator first.
            continue
        lines.append(f"{el.name}--> {_load(val)} {el.load_unit}")
    lines.append("")
    lines.append("[Distances]")
    for el in session.elements:
        if not el.distance_tag:
            continue
        dist = session.distances.get(el.distance_tag)
        if dist is None:
            continue        # never invent 0 — /0 downstream in brf math
        lines.append(f"Distance from {el.name} to center of Balance "
                     f"{el.distance_tag} in inches--> {dist:g}")
    lines.append("")
    lines.append("")
    return lines


def _section_lines(session: CalSession) -> List[str]:
    volt_cols = volt_columns_for(session.kind)
    lines: List[str] = []
    for orient in session.orientations:
        points = session.active_points(orient.key)
        if not points:
            continue
        lines.append(f"[{orient.section}]")
        lines.append(f"Number of Loads--> {len(points)}")
        cols = ", ".join([f"Load[{orient.element.load_unit}]",
                          *(f"{c}[Volt]" for c in volt_cols),
                          "Exct.[Volt]"])
        lines.append(cols)
        for p in points:
            lines.append(", ".join([_load(p.load),
                                    *(_num(v) for v in p.volts),
                                    _num(p.excitation)]))
    return lines


def vol_text(session: CalSession) -> str:
    return "\n".join(_header_lines(session) + _section_lines(session)) + "\n"


def validate_session(session: CalSession) -> List[str]:
    """Completeness warnings the operator should see before writing:
    metadata omitted from the file, or distances whose absence breaks
    the downstream body-frame reduction."""
    warnings: List[str] = []
    missing_max = [el.name for el in session.elements
                   if session.max_loads.get(el.name) is None]
    if missing_max:
        warnings.append("No max load entered for: "
                        + ", ".join(missing_max))
    missing_dist = [el.name for el in session.elements
                    if el.distance_tag
                    and session.distances.get(el.distance_tag) is None]
    if missing_dist:
        warnings.append("No element distance entered for: "
                        + ", ".join(missing_dist)
                        + " (body-frame force reduction needs these)")
    if not session.operator:
        warnings.append("Operator name is empty")
    if not session.serial_number:
        warnings.append("Balance serial number is empty")
    return warnings


def write_vol(session: CalSession, path: str) -> None:
    """Write the session to ``path`` in .vol 3.1 format.

    The text is encoded before the file is opened so a bad character in
    a metadata field can never truncate an existing file on disk.
    """
    data = vol_text(session).encode("ascii")
    with open(path, "wb") as f:
        f.write(data)


# ─────────────────────────────────────────────────────────────────────────
#  Reader — raw, for continuing/editing a calibration
# ─────────────────────────────────────────────────────────────────────────
# Unlike the drivers' balcal.read_vol_file (which folds zero offsets and
# normalizes by excitation for the fit), this keeps the rows exactly as
# stored so a session can be reloaded, points appended or deleted, and
# the file rewritten without loss. Per-point stds are not in the format,
# so reloaded points carry stds=None.

def read_vol_session(path: str) -> CalSession:
    """Load a comma-delimited .vol 3.1 file back into a CalSession."""
    with open(path, "r", encoding="ascii", errors="replace") as f:
        lines = [ln.rstrip("\n") for ln in f]

    s = CalSession()
    i = 0
    section = None

    def kv(line: str):
        parts = line.split("-->", 1)
        return ((parts[0].strip(), parts[1].strip())
                if len(parts) == 2 else (None, None))

    # first pass: balance kind (needed before element lookups)
    for ln in lines:
        if ln.strip().startswith("Balance Type"):
            _k, v = kv(ln)
            s.kind = (BalanceKind.MOMENT
                      if v and re.search(r"5[\s-]*Moment", v)
                      else BalanceKind.FORCE)
            break
    by_name = {el.name: el for el in elements_for(s.kind)}

    while i < len(lines):
        ln = lines[i].strip()
        i += 1
        if not ln:
            continue
        if ln.startswith("["):
            section = re.sub(r"[\[\]]", "", ln).strip()
            m = re.match(r"(.+?)\s+(pos|neg)$", section)
            if m:
                el_name, sense = m.group(1), m.group(2)
                if el_name not in by_name:
                    raise ValueError(
                        f"Unknown element section [{section}] for a "
                        f"{s.kind.value}")
                key = f"{el_name}_{sense}"
                _k, nloads = kv(lines[i].strip())
                i += 2                    # skip count + column header
                for _ in range(int(nloads)):
                    row = lines[i].strip()
                    i += 1
                    vals = [float(v) for v in row.split(",")]
                    if len(vals) != 8:
                        raise ValueError(
                            f"Expected 8 comma-separated values in "
                            f"[{section}], got {len(vals)}: {row!r} "
                            f"(tab-delimited V1 files are not "
                            f"supported)")
                    s.add_point(key, TestPoint(
                        load=vals[0], volts=vals[1:7],
                        excitation=vals[7]))
                section = None
            continue

        k, v = kv(ln)
        if k is None:
            continue
        if section == "Balance Description":
            if "Serial" in k:
                s.serial_number = v
            elif "Diameter" in k:
                s.outer_diameter = v
        elif section == "Maximal Balance Loads":
            if k in by_name:
                try:
                    s.max_loads[k] = float(v.split()[0])
                except (ValueError, IndexError):
                    pass
        elif section == "Distances":
            m = re.search(r"\b(x1|x2|y1|y2)\b.*?$", k)
            tag = m.group(1) if m else None
            if tag:
                try:
                    s.distances[tag] = float(v)
                except ValueError:
                    pass
        elif section is None:
            if k.startswith("Date"):
                m = re.match(r"(\d+)/(\d+)/(\d+)", v)
                if m:
                    mo, d, y = map(int, m.groups())
                    try:
                        s.cal_date = date(y, mo, d)
                    except ValueError:
                        pass
            elif "performed by" in k:
                s.operator = v

    return s
