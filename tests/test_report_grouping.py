"""Speed/mach steps stay separate curves in the Process & Report output.

A sweep that steps mach produces one curve PER STEP. The report used to
plot every point as a single series, which joined the steps into a
zig-zag that reads as noise rather than as three polars.

The split has to key on the COMMANDED setpoint, not the measured Mach:
the measurement is derived from q and drifts point to point, so grouping
on it would make every point its own group. Freestream writes the
setpoint into the run filename and metadata; ``processing`` carries it
into each report point as ``set`` / ``setunit`` and the report's JS
groups on it.
"""

import json
import os
import sys
import time
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "devices", _ROOT.parent / "Streamlined"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

pytest.importorskip("scipy.io")

from freestream import processing                             # noqa: E402
from freestream.config import FreestreamConfig                # noqa: E402
from freestream.manager import DeviceManager                  # noqa: E402
from freestream.recorder import Hdf5Recorder                  # noqa: E402
from freestream.runsheet import build_grid                    # noqa: E402
from freestream.sweep import DONE, SweepEngine                # noqa: E402

MACHS = (0.2, 0.3, 0.4)


def _record_mach_sweep(tmp_path, name="machsteps"):
    """Three mach steps x three alphas through the ATE sim rig."""
    mgr = DeviceManager("SWT-External", sim=True)
    assert mgr.connect_all() == {}
    try:
        for s in mgr.streaming:
            s.start()
        deadline = time.perf_counter() + 10.0
        while mgr.record_blockers() and time.perf_counter() < deadline:
            time.sleep(0.05)
        assert mgr.record_blockers() == [], mgr.record_blockers()

        cfg = FreestreamConfig(
            mode=mgr.mode, sim=True, operator="pytest", config_name=name,
            data_root=str(tmp_path / "runs"), output_format="mat",
            move_timeout_s=60, tunnel_timeout_s=60,
            Sref=18.75, cref=2.86, bref=12.0, model_name="ATE-sim")
        cfg.ref_area, cfg.ref_chord, cfg.ref_span = 18.75, 2.86, 12.0
        rec = Hdf5Recorder(cfg.data_root, config_name=cfg.config_name,
                           output_format="mat")
        eng = SweepEngine(mgr, rec, cfg)
        # specs are start:step:stop
        points = build_grid(alpha_spec="0:3:6", mach_spec="0.2:0.1:0.4",
                            dwell_s=0.1, samples=200)
        assert len(points) == 9, len(points)
        outcomes = eng.run(points)
        assert [o.status for o in outcomes] == [DONE] * len(points), \
            [f"{o.status}:{o.error}" for o in outcomes]
        return Path(rec.config_dir), cfg
    finally:
        mgr.disconnect_all()


@pytest.fixture(scope="module")
def processed(tmp_path_factory):
    """Record and process once — the sweep is the expensive part."""
    run_dir, cfg = _record_mach_sweep(tmp_path_factory.mktemp("mach"))
    paths = processing.process_run(run_dir, config=cfg, facility="SWT",
                                   log=lambda _m: None)
    payload = json.loads(Path(paths["data"]).read_text(encoding="utf-8"))
    html = Path(paths["report"]).read_text(encoding="utf-8")
    return payload, html


# ── the setpoint reaches the report ─────────────────────────────────────
def test_every_point_carries_its_commanded_setpoint(processed):
    payload, _ = processed
    assert len(payload["points"]) == 9
    for p in payload["points"]:
        assert p["set"] is not None, p["run"]
        assert p["setunit"] == "mach"


def test_the_three_mach_steps_are_distinguishable(processed):
    payload, _ = processed
    steps = sorted({p["set"] for p in payload["points"]})
    assert steps == list(MACHS)
    for step in MACHS:
        n = sum(1 for p in payload["points"] if p["set"] == step)
        assert n == 3, f"mach {step} has {n} alphas, expected 3"


def test_setpoint_is_the_command_not_the_measurement(processed):
    """The measured Mach drifts point to point; grouping on it would put
    every point in its own group. The setpoint must be the clean step."""
    payload, _ = processed
    measured = {round(p["mach"], 6) for p in payload["points"]}
    commanded = {p["set"] for p in payload["points"]}
    assert len(commanded) == 3
    assert len(measured) >= len(commanded)


# ── the report knows how to split on it ─────────────────────────────────
def test_report_ships_the_grouping_controls(processed):
    _, html = processed
    for marker in ('id="gsel"', 'id="gnote"', "function autoGroupKey",
                   "function split(", "function groupKeys", "setLabel"):
        assert marker in html, marker


def test_report_payload_is_embedded_not_stubbed(processed):
    _, html = processed
    assert "/*%%DATA%%*/null" not in html


# ── the JS actually groups (run it, do not just grep it) ────────────────
NODE_CHECK = r"""
const fs = require("fs"), vm = require("vm");
const mk = (tag="div") => { const n = {
  tagName: tag, children: [], style: {}, dataset: {}, _text: "", _html: "",
  value: "", checked: false, className: "",
  setAttribute(){}, getAttribute(){return null},
  addEventListener(){}, removeEventListener(){}, click(){},
  appendChild(c){ n.children.push(c); return c },
  removeChild(){}, remove(){}, focus(){}, blur(){},
  querySelector(){ return mk() }, querySelectorAll(){ return [] },
  getBoundingClientRect(){ return {left:0,top:0,width:460,height:300} },
  get textContent(){ return n._text }, set textContent(v){ n._text = String(v) },
  get innerHTML(){ return n._html }, set innerHTML(v){ n._html = String(v) },
}; return n; };
const store = new Map();
globalThis.document = {
  documentElement: mk("html"), body: mk("body"),
  getElementById(id){ if(!store.has(id)) store.set(id, mk()); return store.get(id) },
  createElement: mk, createElementNS: (_n,t)=>mk(t),
  querySelector(){ return mk() }, querySelectorAll(){ return [] },
  addEventListener(){},
};
globalThis.getComputedStyle = () => ({ getPropertyValue: () => "#4fc1ff" });
globalThis.window = { addEventListener(){}, getComputedStyle,
                      matchMedia: () => ({matches:false, addEventListener(){}}) };
globalThis.localStorage = { getItem:()=>null, setItem(){}, removeItem(){} };
globalThis.requestAnimationFrame = (f)=>f();
globalThis.Blob = class {};
globalThis.URL = { createObjectURL:()=>"", revokeObjectURL(){} };

const html = fs.readFileSync(process.argv[2], "utf8");
const src = html.match(/<script>([\s\S]*?)<\/script>/)[1] +
  ";globalThis.__X={EMBED,reduce,split,autoGroupKey,update,el};";
const ctx = vm.createContext(globalThis);
vm.runInContext(src, ctx, {filename:"report.js"});
const X = ctx.__X, P = X.EMBED, g = P.geometry;
const rows = X.reduce(P, {S:g.S, C:g.C, b:g.b, mrc:g.mrc});
const out = {};
out.alphaGroup = X.autoGroupKey(rows, "alpha");
out.alphaSeries = X.split({name:"r", color:"#4fc1ff", rows},
                          out.alphaGroup, "alpha")
  .map(s => ({name: s.name, n: s.rows.length, color: s.color}));
out.speedGroup = X.autoGroupKey(rows, "speed");
out.noneSeries = X.split({name:"r", color:"#4fc1ff", rows}, "none", "alpha").length;
X.el("xsel").value = "alpha"; X.el("gsel").value = "auto"; X.update();
out.noteAuto = X.el("gnote").textContent;
X.el("gsel").value = "none"; X.update();
out.noteNone = X.el("gnote").textContent;
X.el("xsel").value = "speed"; X.el("gsel").value = "auto"; X.update();
out.speedAxisOk = true;
console.log(JSON.stringify(out));
"""


def _node():
    import shutil
    return shutil.which("node")


@pytest.mark.skipif(not _node(), reason="node not available")
def test_report_js_splits_the_curves(processed, tmp_path):
    """Run the report's own script and check what it would draw. A grep
    proves the code shipped; this proves it works."""
    import subprocess
    payload, html = processed
    report = tmp_path / "report.html"
    report.write_text(html, encoding="utf-8")
    harness = tmp_path / "check.js"
    harness.write_text(NODE_CHECK, encoding="utf-8")

    proc = subprocess.run([_node(), str(harness), str(report)],
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    # alpha on x: one curve per mach step, named for it, shaded apart
    assert out["alphaGroup"] == "set"
    assert len(out["alphaSeries"]) == 3, out["alphaSeries"]
    assert [s["n"] for s in out["alphaSeries"]] == [3, 3, 3]
    assert [s["name"] for s in out["alphaSeries"]] == ["M 0.2", "M 0.3",
                                                       "M 0.4"]
    assert len({s["color"] for s in out["alphaSeries"]}) == 3

    # mach on x: the axis already IS the step, so do not split by it
    assert out["speedGroup"] == "none"
    assert out["noneSeries"] == 1

    assert "3 speed step(s), one curve each" == out["noteAuto"]
    assert "one curve" in out["noteNone"]
    assert out["speedAxisOk"]
