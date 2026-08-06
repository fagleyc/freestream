"""Headless post-run reduction + interactive report (Advanced menu).

Drives the sibling Streamlined repo's Qt-free backend
(``utils.windtunnel``) over a just-recorded run directory and writes a
``processed/`` subdirectory beside the raw data containing:

* ``report.html``      — self-contained interactive report: the reduced
  sweep with live geometry/MRC re-reduction in the browser. Per-point
  MEAN balance-element loads (geometry-independent) are embedded, and
  the report's JS re-runs the exact Streamlined elements→BRF→WRF→
  coefficient chain (transforms.py formulas) when Sref/MAC/span/MRC
  change — so MRC shifts update every coefficient live. Other processed
  runs' ``report_data.json`` files can be overlaid for comparison.
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
                                             get_distance_values)

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

    # ── balance cal: run-local .vol → injected matrix ────────────────────
    cal = None
    res = find_run_balance_cal(str(run_dir))
    if res:
        cal = calc_coeffs(read_vol_file(res["vol_path"]),
                          res.get("cal_type", "Linear"))
        log(f"balance cal: {Path(res['vol_path']).name} "
            f"({res.get('cal_type', 'Linear')})")
    if cal is None:
        raw0, _ = read_run_file(str(infos[0].filepath))
        inj = raw0.properties.get("injected_balance_cal")
        if inj:
            cal = balance_cal_from_matrix(
                inj["matrix"], inj.get("cal_type", "Linear"),
                inj.get("distances"), inj.get("serial", ""))
            log("balance cal: injected matrix from run file")
    if cal is None:
        raise RuntimeError(
            "no balance calibration: no run-local .vol and no injected "
            "matrix — stage a .vol beside the run files")

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
    red = reduce_raw(raw_list, cal, geo, pressure_cal={},
                     facility=facility, balance_config=balance_config)
    ss = reduce_steady_state(red)
    df = to_dataframe(ss)

    # ── geometry-independent element means for the live report ──────────
    zero_geo = Geometry(C=C, S=S, b=b, mshift=np.zeros(3))

    def _elem(d):
        if not d:
            return [0.0] * 6
        return [float(v) for v in
                np.mean(calc_brf_forces(d, cal, zero_geo,
                                        balance_config).elements, axis=0)]

    def _mean(d, name, fallback):
        arr = d.get(name)
        return float(np.mean(arr)) if arr is not None and \
            np.size(arr) else float(fallback or 0.0)

    dist = get_distance_values(cal)
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
        "cal": {"file": bal.get("vol_file", res.get("vol_path", "")
                                if res else ""),
                "type": bal.get("cal_type",
                                res.get("cal_type", "Linear")
                                if res else "Linear"),
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

    # ── exports via Streamlined-style writers ────────────────────────────
    cond = {k: np.array([p[k] for p in points])
            for k in ("q", "mach", "re", "uinf", "rho", "T")}
    try:
        import pandas as pd
        with pd.ExcelWriter(paths["xlsx"], engine="openpyxl") as xw:
            df.to_excel(xw, sheet_name="Coefficients", index=False)
            pd.DataFrame({"Alpha": df.get("Alpha"),
                          "Q_psi": cond["q"], "Mach": cond["mach"],
                          "Re": cond["re"], "U_inf_mps": cond["uinf"],
                          "rho_kgm3": cond["rho"], "T_C": cond["T"]}
                         ).to_excel(xw, sheet_name="Conditions",
                                    index=False)
            pd.DataFrame(
                {"key": list(payload["meta"]) + ["facility", "cal_file",
                                                 "S", "C", "b", "MRC"],
                 "value": [str(v) for v in payload["meta"].values()]
                 + [facility, payload["cal"]["file"], S, C, b,
                    str(mrc)]}).to_excel(xw, sheet_name="Meta",
                                         index=False)
        log(f"exported {Path(paths['xlsx']).name}")
    except Exception as exc:                               # noqa: BLE001
        log(f"xlsx export skipped: {exc}")
        paths["xlsx"] = ""

    import scipy.io
    scipy.io.savemat(paths["mat"], {"case_001": {
        "Coefficients": {k: np.asarray(getattr(ss, k)) for k in
                         ("Cl", "Cd", "Cs", "CRoll", "CPitch", "CYaw")},
        "Position": {"alpha": np.asarray(ss.alphas),
                     "beta": np.asarray(ss.betas)},
        "Tunnel_Conditions": {"Q": cond["q"], "Mach": cond["mach"],
                              "Re": cond["re"], "U_inf": cond["uinf"],
                              "rho": cond["rho"], "T": cond["T"]},
        "WRF_Forces": {n: np.array([float(np.mean(getattr(p.wrf_aero, n)))
                                    for p in red])
                       for n in ("Lift", "Drag", "Side", "Roll",
                                 "Pitch", "Yaw")},
        "meta": {"geometry": {"mac": C, "ref_area": S, "span": b,
                              "mrc": np.asarray(mrc)},
                 "facility": facility,
                 "balance_config": balance_config},
    }}, long_field_names=False)
    log(f"exported {Path(paths['mat']).name}")
    log(f"report: {paths['report']}")
    return paths
