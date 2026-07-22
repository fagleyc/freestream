"""DaqBook driver tests — conversion math, config round-trip, sim streaming.

No DLL or hardware required.  Run directly or via pytest.
"""

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daqbook_2000 import daqx
from daqbook_2000.config import ChannelConfig, DaqbookConfig
from daqbook_2000.datamodel import ScanRingBuffer, fields_for
from daqbook_2000.device import Daqbook2000


def test_counts_to_volts_bipolar():
    # gain 1, bipolar: 0 -> -10 V, 32768 -> 0 V, 65535 -> ~ +10 V
    assert daqx.counts_to_volts(0, 1, True) == -10.0
    assert daqx.counts_to_volts(32768, 1, True) == 0.0
    assert abs(daqx.counts_to_volts(65535, 1, True) - 10.0) < 1e-3
    # gain 4 halves twice: ±2.5 V span
    assert daqx.counts_to_volts(0, 4, True) == -2.5


def test_counts_to_volts_unipolar():
    assert daqx.counts_to_volts(0, 1, False) == 0.0
    assert abs(daqx.counts_to_volts(65535, 1, False) - 10.0) < 1e-3
    assert abs(daqx.counts_to_volts(32768, 2, False) - 2.5) < 1e-6


def test_pick_range_matches_rig_setup():
    assert daqx.pick_range(0.0, 3.0, True) == (2, False)    # Pdiff -> 0..5 V
    assert daqx.pick_range(-10.0, 10.0, True) == (1, True)  # Ptot  -> ±10 V
    # Temp is single-ended: unipolar is illegal on SE channels (DaqX err
    # 134 verified on the DaqBook/2005), so 0..10 V SE -> bipolar ±10 V.
    assert daqx.pick_range(0.0, 10.0, False) == (1, True)
    assert daqx.pick_range(0.0, 10.0, True) == (1, False)   # diff may be uni
    assert daqx.pick_range(-0.4, 0.4) == (16, True)          # high gain
    assert daqx.pick_range(-42.0, 42.0) == (1, True)         # fallback widest


def test_channel_flags():
    f = daqx.build_channel_flags(differential=True, bipolar=True)
    assert f & daqx.DafDifferential
    assert f & daqx.DafBipolar
    f = daqx.build_channel_flags(differential=False, bipolar=False)
    assert not (f & daqx.DafDifferential)
    assert not (f & daqx.DafBipolar)


def test_config_roundtrip(tmp_path=None):
    import tempfile
    cfg = DaqbookConfig()
    cfg.scan_hz = 2500.0
    cfg.channels[0].scale = 0.5
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cfg.json"
        cfg.save(p)
        back = DaqbookConfig.load(p)
    assert back.scan_hz == 2500.0
    assert back.channels[0].name == "Pdiff"
    assert back.channels[0].scale == 0.5
    assert len(back.channels) == 3


def test_ring_buffer_block_wrap():
    ring = ScanRingBuffer(fields_for(["A"]), capacity=100)
    for start in range(0, 250, 50):
        ring.push_block({"t": np.arange(start, start + 50, dtype=float),
                         "A": np.arange(start, start + 50, dtype=float),
                         "A_V": np.zeros(50)})
    assert ring.count == 100
    tail = ring.tail(100)
    assert tail["A"][0] == 150.0 and tail["A"][-1] == 249.0


def test_sim_device_streams():
    cfg = DaqbookConfig(force_sim=True, scan_hz=500.0)
    dev = Daqbook2000(cfg)
    blocks = []
    dev.on_block = blocks.append
    try:
        dev.connect()
        dev.start()
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline and dev.frame_count() < 500:
            time.sleep(0.05)
        assert dev.frame_count() >= 500, "sim produced < 1 s of scans"
        latest = dev.latest()
        assert latest is not None
        # engineering conversion applied: Pdiff psid = volts * 0.386949
        assert abs(latest["Pdiff"] -
                   latest["Pdiff_V"] * 0.386949) < 1e-9
        assert blocks, "on_block never fired"
        tail = dev.ring.tail(400)
        dt = np.diff(tail["t"])
        assert abs(np.median(dt) - 1.0 / 500.0) < 1e-4, "bad timebase"
    finally:
        dev.disconnect()
    assert not dev.connected


def test_aux_source_from_sim():
    from daqbook_2000.aux_source import DaqbookAuxSource, PSI_TO_PA
    cfg = DaqbookConfig(force_sim=True, scan_hz=500.0)
    dev = Daqbook2000(cfg)
    try:
        dev.connect()
        dev.start()
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline and dev.frame_count() < 200:
            time.sleep(0.05)
        aux = DaqbookAuxSource(dev)
        q = aux.dynamic_pressure()
        assert q is not None and q > 0
        # sim Pdiff ~1.3 V * 0.386949 psi/V * 6894.76 -> ~3.4 kPa
        assert 1000 < q < 10000
        # Temp default is 1 V = 10 degC; sim reads ~2.95 V -> ~29.5 degC
        t_k = aux.temperature_k()
        assert t_k is not None and 295.0 < t_k < 312.0, f"T={t_k}"
    finally:
        dev.disconnect()


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} daqbook tests passed.")


if __name__ == "__main__":
    _run_all()
