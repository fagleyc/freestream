"""A NI counter-input channel records end to end through freestream.

The point of the counter work: an RPM pickup (or any CI channel) on the
6351 must reach the run files as an ordinary channel — same group, same
per-channel unit metadata — with no special-casing anywhere in the
recorder chain. One sim sweep in the NI LSWT mode proves it.
"""

import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT, _ROOT / "devices"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

scipy_io = pytest.importorskip("scipy.io")

from freestream.config import FreestreamConfig            # noqa: E402
from freestream.manager import DeviceManager              # noqa: E402
from freestream.recorder import Hdf5Recorder              # noqa: E402
from freestream.runsheet import build_grid                # noqa: E402
from freestream.sweep import DONE, SweepEngine            # noqa: E402


def test_ci_channel_lands_in_the_run_file(tmp_path):
    mgr = DeviceManager("LSWT-LSWTSting-NI", sim=True)
    ni = mgr.devices["ni_daq"]
    ni._cfg.ci_channels[0].enabled = True     # stock RPM pickup, ctr0
    assert mgr.connect_all() == {}
    try:
        for s in mgr.streaming:
            s.start()
        deadline = time.perf_counter() + 15.0
        while mgr.record_blockers() and time.perf_counter() < deadline:
            time.sleep(0.05)
        assert mgr.record_blockers() == [], mgr.record_blockers()

        cfg = FreestreamConfig(
            mode=mgr.mode, sim=True, operator="pytest",
            config_name="ni_ctr", data_root=str(tmp_path / "runs"),
            output_format="mat", move_timeout_s=60, tunnel_timeout_s=60)
        rec = Hdf5Recorder(cfg.data_root, config_name=cfg.config_name,
                           output_format="mat")
        eng = SweepEngine(mgr, rec, cfg)
        points = build_grid(alpha_spec="0:2:2", dwell_s=0.2, samples=200)
        outcomes = eng.run(points)
        assert [o.status for o in outcomes] == [DONE] * len(points)
    finally:
        mgr.disconnect_all()

    f = sorted(Path(rec.config_dir).glob("run_*.mat"))[0]
    m = scipy_io.loadmat(str(f), squeeze_me=True, struct_as_record=False)
    group = m["NI_USB_6351"]
    assert "RPM" in group._fieldnames          # rides with the bridges
    rpm = np.asarray(group.RPM, dtype=float)
    assert rpm.size == 200                     # same length as the AI columns
    # sim source wanders around 500 Hz; scale 60 (1 pulse/rev) → ~30 kRPM
    assert 20_000 < float(rpm.mean()) < 40_000

    # the declared unit survives into the per-channel metadata — the
    # engineering value IS the record for counters (pulses/rev is a
    # fixed sensor property, not a calibration to re-derive)
    ch_meta = m["meta"].channels
    grp = getattr(ch_meta, "NI_USB_6351")
    assert str(getattr(grp, "RPM").unit) == "RPM"
