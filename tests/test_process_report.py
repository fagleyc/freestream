"""Advanced ▸ Process & Report — headless Streamlined reduction e2e.

A real LSWT-mode sim sweep records .mat points + manifest, a .vol is
staged beside them (single-vol fallback), then ``processing.process_run``
reduces the directory and writes the processed/ subdirectory
(interactive report + data JSON + xlsx + mat).
"""

import json
import os
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "devices", _ROOT.parent / "Streamlined"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("scipy.io")

from freestream import processing                          # noqa: E402
from freestream.config import FreestreamConfig             # noqa: E402
from freestream.manager import DeviceManager               # noqa: E402
from freestream.recorder import Hdf5Recorder               # noqa: E402
from freestream.runsheet import build_grid                 # noqa: E402
from freestream.sweep import DONE, SweepEngine             # noqa: E402

VOL = _ROOT / "docs" / "cal_files" / "50lb 2026_07_24.vol"


def _wait(cond, timeout=10.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _record_run(tmp_path) -> Path:
    mgr = DeviceManager("LSWT-N-Crescent-NI", sim=True)
    errors = mgr.connect_all()
    assert errors == {}, errors
    try:
        for s in mgr.streaming:
            s.start()
        assert _wait(lambda: mgr.record_blockers() == [], 10.0), \
            mgr.record_blockers()
        cfg = FreestreamConfig(mode="LSWT-N-Crescent-NI", sim=True,
                               operator="pytest", config_name="proc_e2e",
                               data_root=str(tmp_path / "runs"),
                               output_format="mat",
                               move_timeout_s=60, tunnel_timeout_s=60,
                               Sref=18.75, cref=2.86, bref=12.0,
                               MRC_x=1.6, model_name="F16-sim")
        cfg.ref_area, cfg.ref_chord, cfg.ref_span = 18.75, 2.86, 12.0
        rec = Hdf5Recorder(cfg.data_root, config_name=cfg.config_name,
                           output_format="mat")
        points = build_grid(alpha_spec="-2:2:2", dwell_s=0.1,
                            samples=400)
        engine = SweepEngine(mgr, rec, cfg)
        outcomes = engine.run(points)
        assert [o.status for o in outcomes] == [DONE] * len(points), \
            [f"{o.status}:{o.error}" for o in outcomes]
        return Path(rec.config_dir)
    finally:
        mgr.disconnect_all()


def test_process_run_end_to_end(tmp_path):
    run_dir = _record_run(tmp_path)
    assert (run_dir / "manifest.json").exists()
    assert VOL.exists(), VOL
    shutil.copy(VOL, run_dir / VOL.name)       # single-vol fallback cal

    msgs = []
    paths = processing.process_run(run_dir, facility="LSWT",
                                   log=msgs.append)

    out = run_dir / "processed"
    assert out.is_dir()
    report = Path(paths["report"])
    assert report.exists() and report.parent == out
    html = report.read_text(encoding="utf-8")
    assert "/*%%DATA%%*/null" not in html         # payload embedded
    assert "Streamlined" in html

    payload = json.loads(Path(paths["data"]).read_text(encoding="utf-8"))
    assert payload["facility"] == "LSWT"
    assert len(payload["points"]) >= 2
    for p in payload["points"]:
        assert len(p["E"]) == 6 and len(p["off"]["E"]) == 6
        assert np.isfinite(p["q"])
    assert set(payload["cal"]["distances"]) == {"dx1", "dx2",
                                                "dy1", "dy2"}
    assert payload["baseline"]["Cl"], "python baseline missing"

    mat = Path(paths["mat"])
    assert mat.exists()
    import scipy.io
    m = scipy.io.loadmat(mat, squeeze_me=True, struct_as_record=False)
    case = m["case_001"]
    assert hasattr(case, "Coefficients")
    if paths["xlsx"]:                 # openpyxl present on this machine
        assert Path(paths["xlsx"]).exists()


def test_facility_for_mode():
    assert processing.facility_for_mode("LSWT-N-Crescent-NI") == "LSWT"
    assert processing.facility_for_mode("SWT-AC-Internal") == "SWT"
    assert processing.facility_for_mode("") == "SWT"


def test_streamlined_root_found():
    assert processing.streamlined_root() is not None
