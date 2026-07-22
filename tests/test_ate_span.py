"""ATE model-span configuration (full vs ½ span) across the suite.

* the adapter's axes()/channels()/positions() follow the DRIVER's span
  mapping (½ span: alpha ONLY, with the yaw drive's limits);
* a mode2 ½-span sim sweep commands the YAW drive for alpha points,
  never touches the incidence drive, logs a visible warning for the
  dropped beta column, and records span_config="half" in the ROOT attrs
  + the /meta/devices/ate entry, with a constant-zero Beta channel so
  Streamlined's read_hdf5_file keeps its Alpha/Beta access pattern;
* the .mat / .xlsx writers mirror the same attrs;
* flipping the span combo in the embedded panel (DeviceConfigDialog)
  rebinds the LIVE adapter — axes() changes immediately.
"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import sys
import time
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "devices"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "Streamlined"))

from freestream.adapters.ate import AteBalanceAdapter
from freestream.config import FreestreamConfig
from freestream.manager import DeviceManager
from freestream.recorder import Hdf5Recorder
from freestream.runsheet import build_grid
from freestream.sweep import DONE, SweepCallbacks, SweepEngine

from ate_balance import protocol as P


def _wait(cond, timeout=20.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


# ── adapter: axes()/channels() per span_config ───────────────────────────
def test_axes_follow_span_config():
    a = AteBalanceAdapter(sim=True)
    specs = {s.name: s for s in a.axes()}
    assert set(specs) == {"alpha", "beta"}
    assert (specs["alpha"].min, specs["alpha"].max) == P.INC_LIMITS_DEG
    assert (specs["beta"].min, specs["beta"].max) == P.YAW_LIMITS_DEG
    assert a.span_config == "full"
    assert a.extra_meta() == {"span_config": "full",
                              "balance_type": "external"}

    a.config.span_config = "half"          # live rebind — no reconnect
    specs = {s.name: s for s in a.axes()}
    assert set(specs) == {"alpha"}, "½ span must expose alpha ONLY"
    assert (specs["alpha"].min, specs["alpha"].max) == P.YAW_LIMITS_DEG
    assert a.span_config == "half"
    assert a.extra_meta() == {"span_config": "half",
                              "balance_type": "external"}
    # position channels follow: no Beta ChannelSpec in ½ span
    pos_chans = [c.name for c in a.channels() if c.group == "Positioner"]
    assert pos_chans == ["Alpha"]


def test_half_span_moves_yaw_and_rejects_beta():
    a = AteBalanceAdapter(sim=True)
    a.config.span_config = "half"
    a.connect()
    try:
        # alpha 60° is beyond the incidence drive's 45° limit — proves
        # the command reached the YAW drive
        a.move_to(alpha=60.0)
        assert _wait(a.settled), "½-span alpha move did not settle"
        core = a.driver._core
        assert core.yaw_pos == pytest.approx(60.0)
        assert core.inc_pos == pytest.approx(0.0), \
            "incidence drive commanded in ½-span"
        pos = a.positions()
        assert pos["alpha"] == pytest.approx(60.0, abs=0.05)
        assert "beta" not in pos, "½ span has no beta readback"

        with pytest.raises(ValueError, match="beta"):
            a.move_to(beta=5.0)
        with pytest.raises(ValueError, match="outside limits"):
            a.move_to(alpha=95.0)              # yaw drive limits ±90°
        assert a.settled()                     # rejected moves left no flags
    finally:
        a.disconnect()


# ── mode2 ½-span sim sweep: file + metadata + drive routing ─────────────
@pytest.fixture(scope="module")
def half_span_sweep(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("half_span")
    mgr = DeviceManager("mode2", sim=True)
    ate = mgr.devices["ate"]
    ate.config.span_config = "half"
    assert mgr.connect_all() == {}
    events = []
    try:
        for s in mgr.streaming:
            s.start()
        # samples sized so the ~10 Hz Positioner/Tunnel sampling collects
        # enough points for Streamlined's cubic resampler (needs ≥4)
        cfg = FreestreamConfig(mode="mode2", sim=True, operator="span",
                               config_name="half", samples=120,
                               dwell_s=0.05, move_timeout_s=60)
        rec = Hdf5Recorder(tmp_path / "runs", config_name="half")
        engine = SweepEngine(mgr, rec, cfg,
                             SweepCallbacks(on_event=events.append))
        # run sheet WITH a beta column — ½ span must drop it visibly
        points = build_grid(alpha_spec="0:10:10", beta_spec="5",
                            dwell_s=0.05, samples=120)
        assert len(points) == 2
        outcomes = engine.run(points)
        assert [o.status for o in outcomes] == [DONE, DONE], \
            [f"{o.status}:{o.error}" for o in outcomes]
        yield mgr, ate, events, [Path(o.path) for o in outcomes]
    finally:
        for s in mgr.streaming:
            s.stop()
        mgr.disconnect_all()


def test_half_span_sweep_commands_yaw_only(half_span_sweep):
    _mgr, ate, _events, _paths = half_span_sweep
    core = ate.driver._core
    assert core.yaw_pos == pytest.approx(10.0), \
        "alpha points must command the YAW drive"
    assert core.inc_pos == pytest.approx(0.0), \
        "the incidence drive must never be commanded in ½ span"


def test_half_span_sweep_warns_on_dropped_beta(half_span_sweep):
    _mgr, _ate, events, _paths = half_span_sweep
    warned = [e for e in events
              if "WARNING" in e and "beta" in e and "dropped" in e]
    assert warned, f"no dropped-beta warning in events: {events}"
    assert "½-span" in warned[0]


def test_half_span_metadata_inherited(half_span_sweep):
    _mgr, _ate, _events, paths = half_span_sweep
    data = Hdf5Recorder.read_point(paths[-1])
    # (a) HDF5 ROOT attrs
    assert data["attrs"]["span_config"] == "half"
    # (b) /meta/devices/ate entry
    assert data["devices"]["ate"]["span_config"] == "half"
    # Positioner group: yaw-derived Alpha + honest constant-zero Beta
    pos = data["groups"]["Positioner"]
    assert set(pos) == {"Alpha", "Beta"}
    assert pos["Alpha"][-1] == pytest.approx(10.0, abs=0.1)
    assert np.all(pos["Beta"] == 0.0)
    assert data["channel_attrs"]["Positioner"]["Beta"]["unit"] == "deg"


def test_half_span_file_loads_through_streamlined(half_span_sweep):
    _mgr, _ate, _events, paths = half_span_sweep
    from utils.windtunnel.data_io import read_hdf5_file
    raw, _props = read_hdf5_file(str(paths[0]))
    for ch in ("Lift", "Pitch", "Drag", "Side", "Yaw", "Roll",
               "Alpha", "Beta"):
        assert ch in raw.data, f"{ch} missing"
        assert len(raw.data[ch]) == len(raw.time)
    assert np.allclose(raw.data["Beta"], 0.0)
    assert raw.properties["span_config"] == "half"


def test_full_span_sweep_records_span_config_full(tmp_path):
    """Default full span: behaviour unchanged AND the file says so."""
    mgr = DeviceManager("mode2", sim=True)
    assert mgr.connect_all() == {}
    try:
        for s in mgr.streaming:
            s.start()
        cfg = FreestreamConfig(mode="mode2", sim=True, samples=40,
                               dwell_s=0.05, move_timeout_s=60)
        rec = Hdf5Recorder(tmp_path / "runs", config_name="full")
        engine = SweepEngine(mgr, rec, cfg)
        points = build_grid(alpha_spec="5", beta_spec="10",
                            dwell_s=0.05, samples=40)
        outcomes = engine.run(points)
        assert [o.status for o in outcomes] == [DONE]
        core = mgr.devices["ate"].driver._core
        assert core.inc_pos == pytest.approx(5.0)      # alpha → incidence
        assert core.yaw_pos == pytest.approx(10.0)     # beta → yaw
        data = Hdf5Recorder.read_point(outcomes[0].path)
        assert data["attrs"]["span_config"] == "full"
        assert data["devices"]["ate"]["span_config"] == "full"
        assert set(data["groups"]["Positioner"]) == {"Alpha", "Beta"}
    finally:
        for s in mgr.streaming:
            s.stop()
        mgr.disconnect_all()


# ── .mat / .xlsx writers mirror the attrs automatically ─────────────────
_POINT = dict(
    point_meta={"alpha": 10.0},
    blocks={"Positioner": {"Alpha": np.full(5, 10.0),
                           "Beta": np.zeros(5)}},
    rates={"Positioner": 10.0},
    extra_attrs={"mode": "mode2", "span_config": "half"},
    device_meta=[{"id": "ate", "sim": True, "span_config": "half"}],
)


def test_mat_mirrors_span_config(tmp_path):
    scipy_io = pytest.importorskip("scipy.io")
    rec = Hdf5Recorder(tmp_path, config_name="m", output_format="mat")
    path = rec.write_point(**_POINT)
    m = scipy_io.loadmat(str(path), squeeze_me=True,
                         struct_as_record=False)
    meta = m["meta"]
    assert meta.run.span_config == "half"
    assert meta.devices.ate.span_config == "half"


def test_xlsx_mirrors_span_config(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    rec = Hdf5Recorder(tmp_path, config_name="x", output_format="xlsx")
    path = rec.write_point(**_POINT)
    wb = openpyxl.load_workbook(path)
    meta_rows = {r[0].value: r[1].value
                 for r in wb["Meta"].iter_rows(min_row=2)}
    assert meta_rows["span_config"] == "half"
    dev_rows = list(wb["Devices"].iter_rows(values_only=True))
    assert any("span_config" in [str(c) for c in row] or
               "half" in [str(c) for c in row] for row in dev_rows[1:])


# ── embedded panel: the span combo rebinds the LIVE adapter ─────────────
def test_panel_span_combo_rebinds_live_adapter():
    from PyQt6.QtWidgets import QApplication
    from freestream.app.device_config import DeviceConfigDialog

    _app = QApplication.instance() or QApplication([sys.argv[0]])
    a = AteBalanceAdapter(sim=True)
    dlg = DeviceConfigDialog(a)
    try:
        panel = dlg._device_panel
        assert panel is not None, "ate dialog lacks the device panel"
        mp = panel.motion_panel
        # span_config has exactly ONE editor — the Motion tab combo, not
        # a duplicated Settings-form field
        fields = set()
        for form in dlg._forms:
            fields |= set(form.fields())
        assert "span_config" not in fields

        # full span (default): incidence enabled, roles in the titles
        assert mp._inc_box.isEnabled()
        assert "α" in mp._inc_box.title()

        idx = mp.span_combo.findData("half")
        mp.span_combo.setCurrentIndex(idx)     # operator flips the combo
        # the LIVE adapter rebinds immediately (axes()/AxisSpec update)
        assert a.span_config == "half"
        assert [s.name for s in a.axes()] == ["alpha"]
        assert (a.axes()[0].min, a.axes()[0].max) == P.YAW_LIMITS_DEG
        # operator-facing relabel: incidence disabled + marked unused
        assert not mp._inc_box.isEnabled()
        assert "unused" in mp._inc_box.title()
        assert "½-span" in mp._inc_box.toolTip() or \
            "½-span" in mp._inc_box.title()
        assert "α" in mp._yaw_box.title()

        mp.span_combo.setCurrentIndex(mp.span_combo.findData("full"))
        assert a.span_config == "full"
        assert [s.name for s in a.axes()] == ["alpha", "beta"]
        assert mp._inc_box.isEnabled()

        # Cancel reverts a span edit through the config snapshot
        mp.span_combo.setCurrentIndex(idx)
        assert a.span_config == "half"
        dlg.reject()
        assert a.span_config == "full"
    finally:
        dlg._pump.stop()
        dlg._stop_device_panel()
        a.disconnect()
