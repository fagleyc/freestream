"""Headless post-run reduction + interactive report (Advanced menu).

Drives the sibling Streamlined repo's Qt-free backend
(``utils.windtunnel``) over a just-recorded run directory and writes a
``processed/`` subdirectory beside the raw data containing:

* ``report.html``      — self-contained interactive report: the reduced
  sweep with live geometry/MRC re-reduction in the browser. Other
  processed runs' ``report_data.json`` files can be overlaid for
  comparison.

Both balance kinds reduce here, and they take different paths:

internal balance
    The runs carry bridge volts, so a ``.vol`` is required (run-local
    copy, else the matrix injected at record time). Per-point MEAN
    balance ELEMENTS are embedded and the report's JS re-runs the exact
    Streamlined elements→BRF→WRF→coefficient chain.

external balance (the ATE)
    The runs already carry resolved wind-axis loads, so NO calibration
    applies and asking for one is simply wrong. The six resolved loads
    are embedded and the report applies only the MRC moment transfer
    (external_balance.py) and the normalization.
* ``report_data.json``  — the same payload, loadable into another report.
* ``<name>.xlsx``       — coefficients + tunnel conditions + metadata.
* ``<name>.mat``        — Streamlined-style structured export.

Raw data is never touched; the run dir stays the record. Streamlined is
located via the ``FREESTREAM_STREAMLINED_ROOT`` env var or as a sibling
checkout of this repo (…/Streamlined).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[1]


def streamlined_root() -> Optional[Path]:
    env = os.environ.get("FREESTREAM_STREAMLINED_ROOT")
    cands = ([Path(env)] if env else []) + [
        _REPO_ROOT.parent / "Streamlined",
        _REPO_ROOT.parent / "streamlined",
    ]
    for c in cands:
        if (c / "utils" / "windtunnel").is_dir():
            return c
    return None


def ensure_streamlined() -> Path:
    root = streamlined_root()
    if root is None:
        raise RuntimeError(
            "Streamlined repo not found — expected a sibling checkout "
            f"({_REPO_ROOT.parent / 'Streamlined'}) or the "
            "FREESTREAM_STREAMLINED_ROOT environment variable")
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    return root


def facility_for_mode(mode: str) -> str:
    return "LSWT" if str(mode or "").upper().startswith("LSWT") else "SWT"


def _geometry_from(cfg_snap: Dict, config=None) -> Dict:
    """Reference geometry: live config values win over the recorded
    snapshot (both use the run-sheet Sref/cref/bref/MRC_* fields with
    ref_area/ref_chord/ref_span mirrors; 0.0/1.0 are placeholders)."""
    from utils.windtunnel.data_io import reference_geometry_from_config

    geo = reference_geometry_from_config(cfg_snap or {})
    if config is not None:
        live = reference_geometry_from_config(config.to_dict())
        geo.update({k: v for k, v in live.items() if v is not None})
    return geo


def process_run(run_dir, config=None, facility: str = "",
                log: Callable[[str], None] = print) -> Dict[str, str]:
    """Reduce ``run_dir`` headlessly and write the processed/ outputs.

    Returns ``{"report": …, "data": …, "xlsx": …, "mat": …}`` paths.
    Raises RuntimeError with an operator-readable message on any
    unrecoverable problem (no Streamlined, no manifest, no cal…).
    """
    ensure_streamlined()
    import matplotlib
    matplotlib.use("Agg")            # utils.windtunnel imports plotting

    from utils.windtunnel.calibration import (balance_cal_from_matrix,
                                              calc_coeffs, read_vol_file)
    from utils.windtunnel.data_io import (copy_balance_markers,
                                          find_run_balance_cal,
                                          read_run_file,
                                          scan_run_directory)
    from utils.windtunnel.reduction import (reduce_raw,
                                            reduce_steady_state,
                                            to_dataframe)
    from utils.windtunnel.transforms import (Geometry, calc_brf_forces,
                                             get_distance_values,
                                             is_external_balance_data)
    from utils.windtunnel.external_balance import (external_loads_to_ips,
                                                   normalize_span_config,
                                                   SPAN_HALF)

    run_dir = Path(run_dir)
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"no manifest.json in {run_dir}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cfg_snap = manifest.get("config") or {}

    facility = facility or facility_for_mode(cfg_snap.get("mode", ""))
    bal = manifest.get("balance_cal") or {}
    balance_config = (bal.get("balance_config")
                     or cfg_snap.get("balance_config") or "Force")

    infos, mismatches = scan_run_directory(str(run_dir))
    for m in mismatches:
        log(f"manifest mismatch: {m}")
    if not infos:
        raise RuntimeError(f"no run files found in {run_dir}")

    # ── external vs internal balance ─────────────────────────────────────
    # An EXTERNAL balance (the ATE) streams resolved loads in
    # engineering units, so there is no bridge-volts calibration to
    # apply and demanding a .vol is simply wrong. An internal balance
    # streams bridge volts and cannot be reduced without one.
    raw0, _ = read_run_file(str(infos[0].filepath))
    btype = str(bal.get("balance_type")
                or raw0.properties.get("balance_type") or "").lower()
    external = btype == "external" or is_external_balance_data(raw0.data)
    # Model-span configuration decides HOW the ATE's six channels become
    # wind-axis loads: full span leaves the balance level and the
    # channels pass through, ½ span yaws the balance with the model so
    # the channels are body-fixed and permuted. Recorded by the sweep
    # into every run file's root attrs.
    span = normalize_span_config(raw0.properties.get("span_config"))

    cal = None
    if external:
        log("external balance detected — resolved loads, no .vol needed")
        log(f"model span: {span}"
            + (" — channels resolved with the yaw (alpha) rotation"
               if span == SPAN_HALF else
               " — channels are already wind-axis"))
    else:
        res = find_run_balance_cal(str(run_dir))
        if res:
            cal = calc_coeffs(read_vol_file(res["vol_path"]),
                              res.get("cal_type", "Linear"))
            log(f"balance cal: {Path(res['vol_path']).name} "
                f"({res.get('cal_type', 'Linear')})")
        if cal is None:
            inj = raw0.properties.get("injected_balance_cal")
            if inj:
                cal = balance_cal_from_matrix(
                    inj["matrix"], inj.get("cal_type", "Linear"),
                    inj.get("distances"), inj.get("serial", ""))
                log("balance cal: injected matrix from run file")
        if cal is None:
            raise RuntimeError(
                "no balance calibration: this run records bridge volts "
                "from an internal balance, so a .vol is required. Stage "
                "one beside the run files or record with an injected "
                "calibration matrix.")

    geo_d = _geometry_from(cfg_snap, config)
    S = float(geo_d.get("ref_area") or 1.0)
    C = float(geo_d.get("mac") or 1.0)
    b = float(geo_d.get("span") or 1.0)
    mrc = [float(v) for v in (geo_d.get("mrc") or [0.0, 0.0, 0.0])]
    if S == 1.0 and C == 1.0:
        log("WARNING: reference geometry is placeholder (S=C=1) — set "
            "Sref/cref in Measurement Setup for meaningful coefficients")
    geo = Geometry(C=C, S=S, b=b, mshift=np.array(mrc, dtype=float))

    # ── pair AirOn with AirOff (alpha/beta match, first-off fallback) ────
    def _load(info):
        raw, _ = read_run_file(str(info.filepath))
        d = dict(raw.data)
        d["Time"] = raw.time
        return copy_balance_markers(raw, d)

    key = lambda f: (f.alpha, f.beta)                       # noqa: E731
    ons = sorted([i for i in infos if i.air_state == "AirOn"], key=key)
    offs = sorted([i for i in infos if i.air_state == "AirOff"], key=key)
    if not ons:                       # air-off-only set: reduce as-is
        ons, offs = sorted(infos, key=key), []
    off_cache: Dict[str, Dict] = {}

    raw_list, pairs = [], []
    for on in ons:
        m = next((o for o in offs
                  if np.isclose(on.alpha, o.alpha, atol=0.5)
                  and np.isclose(on.beta, o.beta, atol=0.5)), None)
        if m is None and offs:
            m = offs[0]
        d_on = _load(on)
        d_off = off_cache.setdefault(str(m.filepath), _load(m)) \
            if m is not None else {}
        raw_list.append({"AirOn": d_on, "AirOff": d_off})
        pairs.append((on, d_on, m, d_off))

    log(f"reducing {len(raw_list)} points "
        f"({facility}, {balance_config} balance)…")
    # SWT runs the full isentropic chain and needs all three DaqBook
    # channels; LSWT takes q from Pdiff and uses Ptot/Temp only when
    # the Heise supplies them.
    need = ("Pdiff", "Ptot", "Temp") if facility == "SWT" else ("Pdiff",)
    missing = [c for c in need if c not in raw_list[0]["AirOn"]]
    if missing:
        source = ("the DaqBook" if facility == "SWT"
                  else "the Heise (Ptot/Temp) and the NI DAQ (Pdiff)")
        raise RuntimeError(
            f"cannot compute tunnel conditions: the runs carry no "
            f"{', '.join(missing)} channel. Dynamic pressure is "
            f"therefore unknown and no coefficient can be formed. On "
            f"{facility} those channels come from {source}, so record "
            f"with a configuration that includes it.")
    red = reduce_raw(raw_list, cal, geo, pressure_cal={},
                     facility=facility, balance_config=balance_config)
    ss = reduce_steady_state(red)
    df = to_dataframe(ss)

    # ── geometry-independent per-point loads for the live report ────────
    # Internal: the six BALANCE ELEMENTS, from which the browser re-runs
    # elements -> BRF (with MRC) -> WRF -> coefficients.
    # External: the ATE already resolves loads, so the six WIND-AXIS
    # loads are embedded directly and the browser only applies the MRC
    # moment transfer and the normalization.
    zero_geo = Geometry(C=C, S=S, b=b, mshift=np.zeros(3))
    WIRE = ("Lift", "Drag", "Side", "Roll", "Pitch", "Yaw")

    def _elem(d):
        if not d:
            return [0.0] * 6
        if external:
            # RAW channels, converted to the chain's lb / in-lb so the
            # browser's live re-reduction lands on the same numbers as
            # the python baseline below. The mount-dependent resolution
            # into wind axes happens in the report's extResolve().
            c = external_loads_to_ips(d)
            return [float(np.mean(c[k])) if k in c and np.size(c[k])
                    else 0.0 for k in WIRE]
        return [float(v) for v in
                np.mean(calc_brf_forces(d, cal, zero_geo,
                                        balance_config).elements, axis=0)]

    def _mean(d, name, fallback):
        arr = d.get(name)
        return float(np.mean(arr)) if arr is not None and \
            np.size(arr) else float(fallback or 0.0)

    dist = ({"dx1": 0.0, "dx2": 0.0, "dy1": 0.0, "dy2": 0.0}
            if external else get_distance_values(cal))
    points = []
    for i, (on, d_on, off, d_off) in enumerate(pairs):
        t = red[i].tunnel
        points.append({
            "run": int(getattr(on, "run_number", 0) or 0),
            "alpha": _mean(d_on, "Alpha", on.alpha),
            "beta": _mean(d_on, "Beta", on.beta),
            "speed": float(getattr(on, "speed", None)
                           or getattr(on, "speed_value", 0.0) or 0.0),
            "q": float(np.mean(t.Q)), "mach": float(np.mean(t.Mach)),
            "re": float(np.mean(t.Re)), "uinf": float(np.mean(t.U_inf)),
            "rho": float(np.mean(t.rho)), "T": float(np.mean(t.T)),
            "E": _elem(d_on),
            "off": {"alpha": _mean(d_off, "Alpha",
                                   off.alpha if off else on.alpha),
                    "beta": _mean(d_off, "Beta",
                                  off.beta if off else on.beta),
                    "E": _elem(d_off)},
        })

    payload = {
        "name": manifest.get("config_name") or run_dir.name,
        "generated": datetime.now().isoformat(timespec="seconds"),
        "facility": facility, "balance_config": balance_config,
        "mode": "external" if external else "internal",
        "span": span,
        "cal": {"file": ("resolved loads (no .vol)" if external
                         else bal.get("vol_file", "")),
                "type": ("external" if external
                         else bal.get("cal_type", "Linear")),
                "serial": bal.get("balance_serial", ""),
                "distances": {k: float(v) for k, v in dist.items()}},
        "geometry": {"S": S, "C": C, "b": b, "mrc": mrc},
        "meta": {k: cfg_snap.get(k, "") for k in
                 ("model_name", "test_name", "operator", "mode",
                  "config_name")},
        "points": points,
        # python-computed coefficients at the recorded geometry — the
        # in-browser rederivation is cross-checked against these
        "baseline": {c: [float(v) for v in df[c]]
                     for c in ("Alpha", "Beta", "Cl", "Cd", "Cs",
                               "CRoll", "CPitch", "CYaw")
                     if c in df.columns},
    }

    out = run_dir / "processed"
    out.mkdir(exist_ok=True)
    name = payload["name"]
    paths = {"data": str(out / "report_data.json"),
             "report": str(out / "report.html"),
             "xlsx": str(out / f"{name}.xlsx"),
             "mat": str(out / f"{name}.mat")}

    (out / "report_data.json").write_text(
        json.dumps(payload), encoding="utf-8")

    tmpl = (Path(__file__).parent / "report_template.html").read_text(
        encoding="utf-8")
    (out / "report.html").write_text(
        tmpl.replace("/*%%DATA%%*/null", json.dumps(payload)),
        encoding="utf-8")

    # ── exports in Streamlined's OWN shapes (TablePanel writers) ─────────
    # per-point means, tare-subtracted, exactly like the GUI table
    def _m(x):
        return float(np.mean(np.atleast_1d(x)))

    name_case = payload["name"]
    e_names = (list(WIRE) if external
               else (["AftPitch", "AftYaw", "FwdPitch", "FwdYaw",
                      "Axial", "Roll"] if balance_config == "Moment"
                     else ["N1", "N2", "Y1", "Y2", "Axial", "Roll"]))
    rows = []
    for i, r in enumerate(red):
        if external:
            # no bridge elements; report the tared resolved loads under
            # the same slot so the export shape stays constant
            elems = np.array(points[i]["E"], dtype=float) - np.array(
                points[i]["off"]["E"], dtype=float)
        else:
            elems = (np.mean(r.brf_on.elements, axis=0)
                     - np.mean(r.brf_off.elements, axis=0))
        brf_a = ({k: 0.0 for k in ("Fx", "Fy", "Fz", "Mx", "My", "Mz")}
                 if external else
                 {k: _m(getattr(r.brf_on, k)) - _m(getattr(r.brf_off, k))
                  for k in ("Fx", "Fy", "Fz", "Mx", "My", "Mz")})
        cl, cd = _m(r.coeffs.Cl), _m(r.coeffs.Cd)
        rows.append({
            "Alpha": _m(r.alpha), "Beta": _m(r.beta),
            "Cl": cl, "Cd": cd, "Cs": _m(r.coeffs.Cs),
            "CRoll": _m(r.coeffs.CRoll), "CPitch": _m(r.coeffs.CPitch),
            "CYaw": _m(r.coeffs.CYaw),
            "L/D": cl / cd if cd else 0.0,
            "Lift": _m(r.wrf_aero.Lift), "Drag": _m(r.wrf_aero.Drag),
            "Side": _m(r.wrf_aero.Side),
            "M_Roll": _m(r.wrf_aero.Roll),
            "M_Pitch": _m(r.wrf_aero.Pitch),
            "M_Yaw": _m(r.wrf_aero.Yaw),
            "elems": [float(v) for v in elems], "brf": brf_a,
            "Q": _m(r.tunnel.Q), "Mach": _m(r.tunnel.Mach),
            "Re": _m(r.tunnel.Re), "U_inf": _m(r.tunnel.U_inf),
            "rho": _m(r.tunnel.rho), "T": _m(r.tunnel.T),
            "P_tot": _m(r.tunnel.P_tot) / 6894.757,      # Pa → psi
        })
    rows.sort(key=lambda x: (x["Alpha"], x["Beta"]))
    alphas = [x["Alpha"] for x in rows]
    betas = sorted({round(x["Beta"], 1) for x in rows})

    # Excel: TablePanel layout — one sheet per case, key/value header
    # block in cols A/B, data table below (same column labels, IPS)
    try:
        import pandas as pd
        cols = ([("Alpha", "Alpha [deg]"), ("Beta", "Beta [deg]"),
                 ("Cl", "CL"), ("Cd", "CD"), ("Cs", "CY"),
                 ("CRoll", "Cl (roll)"), ("CPitch", "Cm"),
                 ("CYaw", "Cn"), ("L/D", "L/D"),
                 ("Lift", "Lift [lbf]"), ("Drag", "Drag [lbf]"),
                 ("Side", "Side [lbf]"), ("M_Roll", "M_Roll [in-lbf]"),
                 ("M_Pitch", "M_Pitch [in-lbf]"),
                 ("M_Yaw", "M_Yaw [in-lbf]")]
                + [(f"e{i}", f"{n} [lbf]")
                   for i, n in enumerate(e_names)]
                + [("Q", "Q [psi]"), ("Mach", "Mach"), ("Re", "Re"),
                   ("U_inf", "U_inf [m/s]"), ("rho", "rho [kg/m^3]"),
                   ("T", "T [C]"), ("P_tot", "P_tot [psi]")])
        header = [("Case Name", name_case),
                  ("Alpha Range",
                   f"{min(alphas):.1f} to {max(alphas):.1f} deg"),
                  ("Beta Values",
                   ", ".join(f"{v:.1f}" for v in betas) + " deg"),
                  ("Data Points", str(len(rows))),
                  ("Mach", f"{np.mean([x['Mach'] for x in rows]):.4f}"),
                  ("Reynolds Number",
                   f"{np.mean([x['Re'] for x in rows]):.2e}"),
                  ("Dynamic Pressure (Q) [psi]",
                   f"{np.mean([x['Q'] for x in rows]):.4f}"),
                  ("Total Pressure [psi]",
                   f"{np.mean([x['P_tot'] for x in rows]):.4f}")]
        table = {lab: [x[key] if not key.startswith("e")
                       else x["elems"][int(key[1])] for x in rows]
                 for key, lab in cols}
        with pd.ExcelWriter(paths["xlsx"], engine="openpyxl") as xw:
            pd.DataFrame(table).to_excel(
                xw, sheet_name=name_case[:31] or "Case",
                index=False, startrow=len(header) + 1)
            ws = xw.sheets[name_case[:31] or "Case"]
            for i, (k, v) in enumerate(header, start=1):
                ws.cell(row=i, column=1, value=k)
                ws.cell(row=i, column=2, value=v)
        log(f"exported {Path(paths['xlsx']).name} (Streamlined layout)")
    except Exception as exc:                               # noqa: BLE001
        log(f"xlsx export skipped: {exc}")
        paths["xlsx"] = ""

    # MAT: TablePanel categorized case struct + case_index
    def _col(key):
        return np.array([x[key] for x in rows])

    import scipy.io
    run_numbers = np.array([p["run"] for p in points])
    case = {
        "name": name_case, "key": name_case,
        "run_number": int(run_numbers[0]) if run_numbers.size else 0,
        "Tunnel_Conditions": {k: _col(k) for k in
                              ("Q", "Mach", "Re", "U_inf", "rho", "T",
                               "P_tot")},
        "BRF_Forces": {k: np.array([x["brf"][k] for x in rows])
                       for k in ("Fx", "Fy", "Fz", "Mx", "My", "Mz")},
        "WRF_Forces": {"Lift": _col("Lift"), "Drag": _col("Drag"),
                       "Side": _col("Side"), "Roll": _col("M_Roll"),
                       "Pitch": _col("M_Pitch"), "Yaw": _col("M_Yaw")},
        "Coefficients": {k: _col(k) for k in
                         ("Cl", "Cd", "Cs", "CRoll", "CPitch", "CYaw")},
        "Balance_Channels": {n: np.array([x["elems"][i] for x in rows])
                             for i, n in enumerate(e_names)},
        "Position": {"alpha": _col("Alpha"), "beta": _col("Beta")},
        "Geometry": {"MAC": C, "ref_area": S, "span": b,
                     "mrc": np.asarray(mrc)},
        "meta": {"name": name_case,
                 "Mach": float(np.mean(_col("Mach"))),
                 "Reynolds": float(np.mean(_col("Re"))),
                 "Q": float(np.mean(_col("Q"))),
                 "calibration": {"file": payload["cal"]["file"],
                                 "cal_type": payload["cal"]["type"],
                                 "balance_config": balance_config},
                 "geometry": {"mac": C, "ref_area": S, "span": b,
                              "mrc": np.asarray(mrc),
                              "input_units": "IPS",
                              "output_units": "IPS"},
                 "facility": facility},
    }
    scipy.io.savemat(paths["mat"], {
        "case_001": case,
        "case_index": {"keys": [name_case], "names": [name_case],
                       "run_numbers": run_numbers, "count": 1},
    }, long_field_names=False)
    log(f"exported {Path(paths['mat']).name} (Streamlined structure)")
    log(f"report: {paths['report']}")
    return paths
