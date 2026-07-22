"""Tests for freestream.runbook — the 5-sheet run-sheet workbook loader,
run expansion, config round-trip, and an end-to-end sim sweep."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "devices"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Streamlined"))

from freestream.config import FreestreamConfig
from freestream.runbook import (build_run_points, expanded_count,
                                is_runbook_workbook, load_runbook)

openpyxl = pytest.importorskip("openpyxl")


# ── a filled workbook that mirrors the shipped template's layout ───────────
def make_workbook(path, *, runs=None, configs=None, named=None,
                  ref=None, info=None):
    info = info or {}
    ref = ref or {}
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    ti = wb.create_sheet("Test Info")
    ti["B2"] = "TEST / ENTRY INFORMATION"
    pairs = [
        ("Test / entry name", info.get("test_name"),
         "Test objectives", info.get("objectives")),
        ("Facility", info.get("facility", "SSWT"),
         "Balance / ID", info.get("balance")),
        ("Model name / no.", info.get("model_name"),
         "Data file prefix", info.get("data_prefix")),
        ("Test engineer", info.get("engineer"),
         "Operator(s)", info.get("operator")),
    ]
    for i, (lb, lv, rb, rv) in enumerate(pairs, start=4):
        ti.cell(i, 2, lb)
        ti.cell(i, 3, lv)
        ti.cell(i, 5, rb)
        ti.cell(i, 6, rv)
    ti.cell(13, 2, "MODEL REFERENCE DIMENSIONS  (used for coefficient reduction)")
    hdr = ("quantity", "symbol", "value", "units", "notes")
    for c, text in enumerate(hdr, start=2):
        ti.cell(14, c, text)
    ref_rows = [("Reference area", "Sref", ref.get("Sref"), "in^2"),
                ("Reference chord (MAC)", "cref", ref.get("cref"), "in"),
                ("Reference span", "bref", ref.get("bref"), "in"),
                ("Moment ref center X", "MRC_x", ref.get("MRC_x"), "in"),
                ("Moment ref center Y", "MRC_y", ref.get("MRC_y"), "in"),
                ("Moment ref center Z", "MRC_z", ref.get("MRC_z"), "in")]
    for i, (qty, sym, val, unit) in enumerate(ref_rows, start=15):
        ti.cell(i, 2, qty)
        ti.cell(i, 3, sym)
        ti.cell(i, 4, val)
        ti.cell(i, 5, unit)

    rm = wb.create_sheet("Run Matrix")
    rm.append(["Run", None, "Test Parameters", None, None,
               "Acquisition", None, "Model Config", "Notes"])
    rm.append(["run", "enable", "alpha (deg)", "beta (deg)", "mach",
               "samples", "sample_rate_hz", "config", "notes"])
    for row in (runs or []):
        rm.append(row)

    mc = wb.create_sheet("Model Configs")
    mc.append(["MODEL CONFIGURATIONS — define each config once."])
    mc.append([])
    cfg_headers = ["config_name", "flap_deg", "gear", "trip", "config_notes"]
    mc.append(cfg_headers)
    for row in (configs or []):
        mc.append(row)

    na = wb.create_sheet("Named Arrays")
    na.append(["NAMED ARRAYS — reusable sweeps."])
    na.append([])
    na.append(["name", "definition", "expands to", "notes"])
    for row in (named or []):
        na.append(row)

    wb.save(path)
    return path


DEFAULT_CONFIGS = [
    ["clean", 0, "up", "free", "baseline"],
    ["flaps10", 10, "down", "free", "flaps 10"],
]
DEFAULT_NAMED = [["alpha_fine", "-4:1:16", "-4..16", "1-deg"]]
DEFAULT_RUNS = [
    ["run_a", "Y", "-4:2:16", "0", "0.3,0.5,0.7", 1000, 2000, "clean",
     "mach sweep"],
    ["run_b", "N", "0,2,4", "0", "0.3", 500, 1000, "flaps10", "disabled"],
]


@pytest.fixture
def book(tmp_path):
    path = make_workbook(
        tmp_path / "rs.xlsx", runs=DEFAULT_RUNS, configs=DEFAULT_CONFIGS,
        named=DEFAULT_NAMED,
        ref={"Sref": 2.5, "cref": 0.5, "bref": 5.0,
             "MRC_x": 1.0, "MRC_y": 0.0, "MRC_z": 0.25},
        info={"test_name": "T-1", "model_name": "NACA0012",
              "engineer": "Casey", "operator": "cadet",
              "objectives": "polar", "data_prefix": "n12"})
    return load_runbook(path)


class TestParse:
    def test_is_workbook(self, tmp_path):
        path = make_workbook(tmp_path / "w.xlsx", runs=DEFAULT_RUNS,
                             configs=DEFAULT_CONFIGS)
        assert is_runbook_workbook(path)

    def test_test_info_and_ref_dims(self, book):
        info = book.friendly_info()
        assert info["test_name"] == "T-1"
        assert info["model_name"] == "NACA0012"
        assert info["engineer"] == "Casey"
        assert book.ref_dim_value("Sref") == 2.5
        assert book.ref_dim_value("cref") == 0.5
        assert set(book.ref_dims) == {"Sref", "cref", "bref",
                                      "MRC_x", "MRC_y", "MRC_z"}

    def test_configs_verbatim(self, book):
        assert set(book.configs) == {"clean", "flaps10"}
        assert book.configs["flaps10"]["flap_deg"] == 10
        assert book.configs["flaps10"]["gear"] == "down"

    def test_named_arrays(self, book):
        assert book.named_arrays == {"alpha_fine": "-4:1:16"}

    def test_runs_and_disabled(self, book):
        assert [r.run for r in book.runs] == ["run_a", "run_b"]
        assert book.runs[0].enable is True
        assert book.runs[1].enable is False
        assert [r.run for r in book.enabled_runs] == ["run_a"]
        assert book.runs[0].samples == 1000
        assert book.runs[0].sample_rate_hz == 2000


class TestBuildRunPoints:
    def test_run_a_matrix(self, book):
        run = book.runs[0]
        pts = build_run_points(book, run)
        assert len(pts) == 44                       # 11 alpha × 4 mach
        assert expanded_count(book, run) == 44

    def test_mach_outer_air_off_first(self, book):
        pts = build_run_points(book, book.runs[0])
        machs = [p.mach for p in pts]
        assert machs[0] == 0                        # air-off first
        # Mach is the OUTER loop: the first 11 points are all air-off
        assert all(m == 0 for m in machs[:11])
        assert sorted(set(machs)) == [0, 0.3, 0.5, 0.7]

    def test_config_columns_and_refdims_in_meta(self, book):
        pts = build_run_points(book, book.runs[0])
        meta = pts[0].meta
        assert meta["config_name"] == "clean"
        assert meta["flap_deg"] == 0
        assert meta["gear"] == "up"
        assert meta["run"] == "run_a"
        assert meta["Sref"] == 2.5
        assert meta["cref"] == 0.5
        assert meta["model_name"] == "NACA0012"

    def test_samples_override(self, book):
        pts = build_run_points(book, book.runs[0])
        assert all(p.samples == 1000 for p in pts)


class TestConfigRoundTrip:
    def test_new_fields_round_trip(self):
        cfg = FreestreamConfig(test_name="T-9", model_name="M", engineer="C",
                               data_prefix="p", objectives="o", facility="F",
                               Sref=3.1, cref=0.7, bref=6.0,
                               MRC_x=1.1, MRC_y=0.2, MRC_z=0.3)
        back = FreestreamConfig.from_dict(cfg.to_dict())
        assert back.test_name == "T-9"
        assert back.model_name == "M"
        assert back.Sref == 3.1
        assert back.cref == 0.7
        assert back.MRC_x == 1.1
        assert back.MRC_z == 0.3


class TestEndToEndSweep:
    def test_sweep_writes_config_and_refdims_into_attrs(self, tmp_path):
        from freestream.manager import DeviceManager
        from freestream.recorder import Hdf5Recorder, read_point
        from freestream.sweep import DONE, SweepEngine

        # a small no-tunnel run (mach blank → single air-off level, alpha 0/2)
        path = make_workbook(
            tmp_path / "e2e.xlsx",
            runs=[["run_1", "Y", "0:2:2", "", "", 120, 200, "clean",
                   "e2e"]],
            configs=[["clean", 5, "down", "grit", "flapped"]],
            ref={"Sref": 2.5, "cref": 0.5, "bref": 5.0,
                 "MRC_x": 1.0, "MRC_y": 0.0, "MRC_z": 0.25},
            info={"model_name": "NACA0012"})
        book = load_runbook(path)
        pts = build_run_points(book, book.runs[0])
        assert len(pts) == 2                         # alpha 0,2 × mach {0}

        mgr = DeviceManager("mode1", sim=True)
        assert mgr.connect_all() == {}
        try:
            for s in mgr.streaming:
                s.start()
            cfg = FreestreamConfig(mode="mode1", sim=True,
                                   config_name="e2e_rb", samples=120,
                                   dwell_s=0.05, move_timeout_s=60,
                                   tunnel_timeout_s=60)
            rec = Hdf5Recorder(tmp_path / "runs", config_name=cfg.config_name)
            engine = SweepEngine(mgr, rec, cfg)
            outcomes = engine.run(pts)
            assert [o.status for o in outcomes] == [DONE, DONE], \
                [f"{o.status}:{o.error}" for o in outcomes]
        finally:
            for s in mgr.streaming:
                s.stop()
            mgr.disconnect_all()

        data = read_point(outcomes[0].path)
        attrs = data["attrs"]
        # model-config columns rode into the HDF5 root attrs verbatim
        assert attrs["flap_deg"] == 5
        assert attrs["gear"] == "down"
        assert attrs["run"] == "run_1"
        assert attrs["model_name"] == "NACA0012"
        # reference dimensions for downstream coefficient reduction
        assert float(attrs["Sref"]) == 2.5
        assert float(attrs["cref"]) == 0.5
        assert float(attrs["MRC_x"]) == 1.0
