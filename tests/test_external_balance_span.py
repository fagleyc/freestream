"""½-span vs full-span external-balance runs, end to end.

The ATE yaws with the model but never pitches with it, so a semispan
model on the turntable (alpha = the yaw drive) hands the reduction
body-fixed channels, while a full-span model on the incidence strut
hands it wind-axis ones. Freestream is where the mount is configured,
so Freestream is what has to record it — a run file without the marker
leaves Streamlined guessing, and guessing wrong is what made ½-span
drag come out backwards.

Also covers the load-unit stamp: the OGI's engineering-unit setting is
operator-selectable and never appears on the wire, so the recorded
channel unit attributes are the only thing telling the reduction
whether it is looking at newtons or pounds.
"""

import json
import os
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

from ate_balance.config import AteConfig, LOAD_UNITS          # noqa: E402
from freestream import processing                             # noqa: E402
from freestream.config import FreestreamConfig                # noqa: E402
from freestream.manager import DeviceManager                  # noqa: E402
from freestream.recorder import Hdf5Recorder                  # noqa: E402
from freestream.runsheet import build_grid                    # noqa: E402
from freestream.sweep import DONE, SweepEngine                # noqa: E402

ATE_MODE = "SWT-External"


def _wait(cond, timeout=10.0):
    end = time.perf_counter() + timeout
    while time.perf_counter() < end:
        if cond():
            return True
        time.sleep(0.05)
    return False


def _record(tmp_path, config_name, span="full", load_units="N"):
    """Record a short ATE sim sweep with the given mount + unit setting."""
    mgr = DeviceManager(ATE_MODE, sim=True)
    ate = mgr.devices["ate"]
    ate._cfg.span_config = span              # before connect: axes follow
    ate._cfg.load_units = load_units
    errors = mgr.connect_all()
    assert errors == {}, errors
    try:
        for s in mgr.streaming:
            s.start()
        assert _wait(lambda: mgr.record_blockers() == [], 10.0), \
            mgr.record_blockers()
        cfg = FreestreamConfig(
            mode=mgr.mode, sim=True, operator="pytest",
            config_name=config_name, data_root=str(tmp_path / "runs"),
            output_format="mat", move_timeout_s=60, tunnel_timeout_s=60,
            Sref=18.75, cref=2.86, bref=12.0, model_name="ATE-sim")
        cfg.ref_area, cfg.ref_chord, cfg.ref_span = 18.75, 2.86, 12.0
        rec = Hdf5Recorder(cfg.data_root, config_name=cfg.config_name,
                           output_format="mat")
        eng = SweepEngine(mgr, rec, cfg)
        points = build_grid(alpha_spec="0:6:6", dwell_s=0.1, samples=200)
        outcomes = eng.run(points)
        assert [o.status for o in outcomes] == [DONE] * len(points), \
            [f"{o.status}:{o.error}" for o in outcomes]
        return Path(rec.config_dir), cfg
    finally:
        mgr.disconnect_all()


# ── the driver config ───────────────────────────────────────────────────
def test_load_units_defaults_to_newtons_and_is_validated():
    assert AteConfig().load_units == "N"
    assert set(LOAD_UNITS) == {"N", "lb", "kg"}
    # the OGI pairs pounds with FEET, not inches — the whole reason the
    # marker has to distinguish them
    assert LOAD_UNITS["lb"] == ("lbf", "lbf*ft")
    with pytest.raises(ValueError, match="load_units"):
        AteConfig(load_units="slugs")


def test_load_units_round_trips_through_json(tmp_path):
    cfg = AteConfig(load_units="lb")
    path = tmp_path / "ate.json"
    cfg.save(path)
    assert AteConfig.load(path).load_units == "lb"


def test_adapter_stamps_the_configured_units_on_its_channels():
    from freestream.adapters.ate import AteBalanceAdapter
    for setting, (force_u, moment_u) in LOAD_UNITS.items():
        ad = AteBalanceAdapter(sim=True)
        ad._cfg.load_units = setting
        units = {c.name: c.unit for c in ad.channels()}
        assert units["Lift"] == force_u, setting
        assert units["Pitch"] == moment_u, setting
        assert ad.extra_meta()["load_units"] == setting


# ── the recorded marker ─────────────────────────────────────────────────
@pytest.mark.parametrize("span", ["full", "half"])
def test_span_config_lands_in_every_run_file(tmp_path, span):
    import scipy.io
    run_dir, _ = _record(tmp_path, f"span_{span}", span=span)
    files = sorted(run_dir.glob("run_*.mat"))
    assert files
    for f in files:
        m = scipy.io.loadmat(str(f), squeeze_me=True,
                             struct_as_record=False)
        assert str(m["meta"].run.span_config) == span, f.name


def test_half_span_run_records_beta_as_a_constant_zero(tmp_path):
    """½ span has no beta axis, but the column stays in the file as a
    recorded zero so downstream readers keep a uniform shape — the
    span_config marker is what says the zero is structural."""
    import scipy.io
    run_dir, _ = _record(tmp_path, "half_axes", span="half")
    m = scipy.io.loadmat(str(sorted(run_dir.glob("run_*.mat"))[0]),
                         squeeze_me=True, struct_as_record=False)
    pos = m["Positioner"]
    assert "Alpha" in pos._fieldnames and "Beta" in pos._fieldnames
    assert np.allclose(np.asarray(pos.Beta, dtype=float), 0.0)
    assert str(m["meta"].run.span_config) == "half"


def test_streamlined_probe_reads_the_recorded_span(tmp_path):
    from utils.windtunnel.data_io import run_span_config
    for span in ("full", "half"):
        run_dir, _ = _record(tmp_path, f"probe_{span}", span=span)
        assert run_span_config(str(run_dir)) == span


def test_recorded_units_classify_for_the_reduction(tmp_path):
    """The unit stamp has to survive the round trip as a marker the
    reducer understands, including lb-with-FEET."""
    from utils.windtunnel.data_io import read_run_file
    expected = {"N": "N", "lb": "lbft", "kg": "kg"}
    for setting, marker in expected.items():
        run_dir, _ = _record(tmp_path, f"units_{setting}",
                             load_units=setting)
        raw, _ = read_run_file(str(sorted(run_dir.glob("run_*.mat"))[0]))
        assert raw.properties.get("load_units") == marker, setting


# ── processing ──────────────────────────────────────────────────────────
def test_processing_carries_the_span_into_the_report(tmp_path):
    run_dir, cfg = _record(tmp_path, "half_report", span="half")
    msgs = []
    paths = processing.process_run(run_dir, config=cfg, facility="SWT",
                                   log=msgs.append)
    assert any("model span: half" in m for m in msgs), msgs

    payload = json.loads(Path(paths["data"]).read_text(encoding="utf-8"))
    assert payload["mode"] == "external"
    assert payload["span"] == "half"

    html = Path(paths["report"]).read_text(encoding="utf-8")
    assert "extResolve" in html            # the mount-aware resolver
    assert 'id="c-span"' in html           # and the chip that names it


def test_full_and_half_reduce_to_different_coefficients(tmp_path):
    """Same rig, same sim loads — only the declared mount differs, and
    the answer must too. Equal coefficients would mean the marker is
    being ignored."""
    out = {}
    for span in ("full", "half"):
        run_dir, cfg = _record(tmp_path, f"cmp_{span}", span=span)
        paths = processing.process_run(run_dir, config=cfg,
                                       facility="SWT", log=lambda _m: None)
        payload = json.loads(
            Path(paths["data"]).read_text(encoding="utf-8"))
        out[span] = payload

    assert out["full"]["span"] == "full" and out["half"]["span"] == "half"
    cl_full = np.asarray(out["full"]["baseline"]["Cl"], dtype=float)
    cl_half = np.asarray(out["half"]["baseline"]["Cl"], dtype=float)
    assert cl_full.size and cl_full.size == cl_half.size
    assert not np.allclose(cl_full, cl_half), \
        "the span marker changed nothing — it is not reaching the reduction"


def test_embedded_loads_are_in_the_reductions_units(tmp_path):
    """The browser re-runs the chain from the embedded per-point loads,
    so those have to be in the same lb / in-lb the python baseline used
    — otherwise the live sliders land 4.45x off the printed curves."""
    run_dir, cfg = _record(tmp_path, "unit_parity", load_units="N")
    paths = processing.process_run(run_dir, config=cfg, facility="SWT",
                                   log=lambda _m: None)
    payload = json.loads(Path(paths["data"]).read_text(encoding="utf-8"))

    import scipy.io
    from utils.windtunnel.external_balance import N_TO_LBF

    # match on run number: the directory holds air-off points too, and
    # payload["points"] lists only the air-on ones
    point = payload["points"][0]
    by_run = {}
    for f in sorted(run_dir.glob("run_*.mat")):
        m = scipy.io.loadmat(str(f), squeeze_me=True,
                             struct_as_record=False)
        by_run[int(m["meta"].run.run_number)] = m
    m = by_run[int(point["run"])]
    lift_n = float(np.mean(np.asarray(m["ATE_Balance"].Lift, dtype=float)))
    # rtol allows for the slow-channel resample onto the DaqBook time
    # base; a unit slip would be 4.45x, not 0.01%
    assert np.isclose(point["E"][0],        # WIRE order: Lift first
                      lift_n * N_TO_LBF, rtol=1e-3)
