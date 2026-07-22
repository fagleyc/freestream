"""NI USB-6351 driver tests — range snapping, config round-trip, balance
layout rename, sim streaming/tare/AO, hardware task configuration against a
recording fake nidaqmx (no NI-DAQmx install, no hardware).
"""

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ni_usb_6351 import device as ni_device
from ni_usb_6351.config import (AI_RANGES_V, ChannelConfig, NiDaqConfig,
                                pick_range)
from ni_usb_6351.device import NiUsb6351
from ni_usb_6351.emulator import SimCore


# ── config ───────────────────────────────────────────────────────────────
def test_pick_range_snapping():
    # requested spans snap UP to the smallest covering native range
    assert pick_range(-0.15, 0.15) == 0.2
    assert pick_range(-0.05, 0.05) == 0.1          # smallest native range
    assert pick_range(-0.2, 0.2) == 0.2            # exact boundary holds
    assert pick_range(-0.3, 0.7) == 1.0            # asymmetric uses max |v|
    assert pick_range(-12.0, 12.0) == 10.0         # caps at widest range
    assert pick_range(-100.0, 100.0) == AI_RANGES_V[-1]
    # ChannelConfig.native_range goes through the same picker
    assert ChannelConfig(v_min=-0.15, v_max=0.15).native_range == 0.2


def test_default_channel_set():
    cfg = NiDaqConfig()
    names = [c.name for c in cfg.channels]
    assert names == ["N1", "N2", "Y1", "Y2", "Axial", "Roll",
                     "Excitation", "Spare"]
    for c in cfg.channels[:6]:                     # bridges + Axial/Roll
        assert c.balance and c.enabled and c.terminal == "DIFF"
    assert [c.native_range for c in cfg.channels[:6]] == \
        [0.2, 0.2, 0.2, 0.2, 0.5, 0.5]
    exc = cfg.channels[6]
    assert not exc.balance and exc.enabled and exc.native_range == 10.0
    spare = cfg.channels[7]
    assert not spare.enabled and not spare.balance
    # scale is display-only and defaults to 1.0 (recorded data = raw volts)
    assert all(c.scale == 1.0 for c in cfg.channels)
    # both AOs present but disabled by default
    assert [a.name for a in cfg.ao_channels] == ["AO0", "AO1"]
    assert not any(a.enabled for a in cfg.ao_channels)


def test_enabled_channels_excludes_disabled_and_blank():
    cfg = NiDaqConfig()
    assert [c.name for c in cfg.enabled_channels()] == \
        ["N1", "N2", "Y1", "Y2", "Axial", "Roll", "Excitation"]  # no Spare
    cfg.channels[1].enabled = False                # kill N2
    cfg.channels[2].name = "   "                   # blank Y1
    assert [c.name for c in cfg.enabled_channels()] == \
        ["N1", "Y2", "Axial", "Roll", "Excitation"]
    # same filter on the AO side
    assert cfg.enabled_ao_channels() == []
    cfg.ao_channels[0].enabled = True
    assert [a.name for a in cfg.enabled_ao_channels()] == ["AO0"]
    cfg.ao_channels[0].name = ""
    assert cfg.enabled_ao_channels() == []


def test_config_json_roundtrip():
    import tempfile
    cfg = NiDaqConfig()
    cfg.scan_hz = 2000.0
    cfg.device_name = "Dev7"
    cfg.trigger.mode = "digital_edge"
    cfg.trigger.source = "PFI3"
    cfg.trigger.edge = "falling"
    cfg.trigger.level_v = 2.5
    cfg.ao_channels[0].enabled = True
    cfg.ao_channels[0].static_v = 1.5
    cfg.ao_channels[0].waveform = "sine"
    cfg.channels[7].enabled = True                 # wire the Spare tunnel ch
    cfg.channels[7].name = "Tunnel"
    cfg.channels[7].scale = 2.0
    cfg.channels[7].unit = "psi"
    del cfg.channels[5]                            # drop Roll entirely
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cfg.json"
        cfg.save(p)
        back = NiDaqConfig.load(p)
    assert back.scan_hz == 2000.0 and back.device_name == "Dev7"
    assert back.trigger.mode == "digital_edge"
    assert back.trigger.source == "PFI3"
    assert back.trigger.edge == "falling" and back.trigger.level_v == 2.5
    assert back.ao_channels[0].enabled
    assert back.ao_channels[0].static_v == 1.5
    assert back.ao_channels[0].waveform == "sine"
    assert len(back.channels) == 7
    assert [c.name for c in back.channels] == \
        ["N1", "N2", "Y1", "Y2", "Axial", "Excitation", "Tunnel"]
    tun = back.channels[-1]
    assert tun.enabled and tun.scale == 2.0 and tun.unit == "psi"


def test_from_dict_tolerates_unknown_keys():
    # forward compat: a config written by a NEWER build must still load
    d = NiDaqConfig().to_dict()
    d["future_top_level"] = {"nested": True}
    d["channels"][0]["mystery_field"] = 42
    d["ao_channels"][0]["someday"] = "maybe"
    d["trigger"]["retrigger_count"] = 3
    cfg = NiDaqConfig.from_dict(d)
    assert cfg.channels[0].name == "N1"
    assert cfg.trigger.mode == "immediate"
    assert len(cfg.ao_channels) == 2


# ── balance layout (Force ↔ Moment) ──────────────────────────────────────
def test_balance_layout_rename():
    cfg = NiDaqConfig()
    renames = cfg.set_balance_config("Moment")
    assert renames == {"N1": "AftPitch", "N2": "AftYaw",
                       "Y1": "FwdPitch", "Y2": "FwdYaw"}
    assert cfg.balance_config == "Moment"
    assert [c.name for c in cfg.channels] == \
        ["AftPitch", "AftYaw", "FwdPitch", "FwdYaw", "Axial", "Roll",
         "Excitation", "Spare"]
    # idempotent: re-applying the same layout renames nothing
    assert cfg.set_balance_config("Moment") == {}
    # and back again restores the Force names
    assert cfg.set_balance_config("Force") == \
        {"AftPitch": "N1", "AftYaw": "N2", "FwdPitch": "Y1", "FwdYaw": "Y2"}
    try:
        cfg.set_balance_config("Sideways")
        assert False, "junk balance_config must raise"
    except ValueError:
        pass


# ── sim streaming ────────────────────────────────────────────────────────
def _wait_frames(dev, n, timeout=5.0):
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline and dev.frame_count() < n:
        time.sleep(0.05)
    return dev.frame_count()


def test_sim_device_streams_tare_and_live_rename():
    cfg = NiDaqConfig(force_sim=True, scan_hz=500.0)
    dev = NiUsb6351(cfg)
    try:
        dev.connect()
        assert dev.connected and dev.sim_mode
        dev.start()
        assert _wait_frames(dev, 200) >= 200

        for f in ("t", "N1", "N1_V", "Excitation", "Excitation_V"):
            assert f in dev.ring.fields
        latest = dev.latest()
        assert latest is not None
        # scale is 1.0 on bridges — display value IS the raw volts
        assert abs(latest["N1"] - latest["N1_V"]) < 1e-9
        assert abs(latest["N1_V"]) < 0.2               # inside native range
        assert 9.5 < latest["Excitation"] < 10.5

        # tare pulls the bridge mean to ~0 without touching excitation
        before = float(np.mean(dev.ring.tail(200)["N1_V"]))
        dev.tare(seconds=0.3)
        time.sleep(0.5)
        after = float(np.mean(dev.ring.tail(100)["N1_V"]))
        assert abs(after) < 1e-3
        assert 9.5 < dev.latest()["Excitation"] < 10.5

        # clear_tare restores the raw (offset-bearing) signal
        dev.clear_tare()
        time.sleep(0.5)
        restored = float(np.mean(dev.ring.tail(100)["N1_V"]))
        assert abs(restored - before) < 1e-3

        # live layout switch renames the ring fields and keeps streaming
        fc = dev.frame_count()
        renames = dev.set_balance_config("Moment")
        assert renames["N1"] == "AftPitch"
        assert "AftPitch" in dev.ring.fields and "N1" not in dev.ring.fields
        assert _wait_frames(dev, fc + 100) >= fc + 100
        latest = dev.latest()
        assert "AftPitch" in latest and "AftPitch_V" in latest
        assert "N1" not in latest

        dev.stop()
        assert not dev.running
    finally:
        dev.disconnect()
    assert not dev.connected


def test_sim_frame_count_resets_on_reconnect():
    cfg = NiDaqConfig(force_sim=True, scan_hz=500.0)
    dev = NiUsb6351(cfg)
    try:
        dev.connect()
        dev.start()
        fc1 = _wait_frames(dev, 100)
        assert fc1 >= 100
        # monotonic while connected
        seen = [dev.frame_count()]
        for _ in range(5):
            time.sleep(0.05)
            seen.append(dev.frame_count())
        assert all(b >= a for a, b in zip(seen, seen[1:]))
        dev.disconnect()
        assert not dev.connected

        # connect() zeroes _scan_total: the count restarts from 0
        dev.connect()
        assert dev.frame_count() < fc1
        dev.start()
        assert _wait_frames(dev, 100) >= 100
    finally:
        dev.disconnect()


def test_sim_analog_output():
    cfg = NiDaqConfig(force_sim=True, scan_hz=500.0)
    cfg.ao_channels[0].enabled = True
    dev = NiUsb6351(cfg)
    try:
        dev.connect()
        assert dev.ao_names() == ["AO0"]

        assert dev.set_ao("AO0", 2.0) == 2.0
        assert cfg.ao_channels[0].static_v == 2.0

        # clamps to the channel's v_min/v_max
        assert dev.set_ao("AO0", 99.0) == cfg.ao_channels[0].v_max == 10.0
        assert cfg.ao_channels[0].static_v == 10.0
        assert dev.set_ao("AO0", -99.0) == cfg.ao_channels[0].v_min == -10.0

        try:
            dev.set_ao("AO9", 1.0)
            assert False, "unknown AO name must raise"
        except ValueError:
            pass

        # waveform start/stop are sim no-ops (no task, no exception)
        dev.start_ao_wave()
        assert not dev.ao_wave_running
        dev.stop_ao_wave()

        dev.zero_ao()
        assert cfg.ao_channels[0].static_v == 0.0
    finally:
        dev.disconnect()


# ── hardware-config correctness against a recording fake nidaqmx ─────────
class _FakeInStream:
    avail_samp_per_chan = 0


class _FakeTask:
    """Records every DAQmx call the driver makes; produces no data."""

    def __init__(self, name="", fail_timing=False):
        self.name = name
        self.fail_timing = fail_timing
        self.ai_calls = []
        self.ao_calls = []
        self.timing_calls = []
        self.dig_trig_calls = []
        self.anlg_trig_calls = []
        self.writes = []
        self.started = False
        self.stopped = False
        self.closed = False
        self.in_stream = _FakeInStream()
        task = self

        class _AI:
            def add_ai_voltage_chan(self, physical,
                                    name_to_assign_to_channel="",
                                    terminal_config=None,
                                    min_val=None, max_val=None):
                task.ai_calls.append({
                    "physical": physical,
                    "name": name_to_assign_to_channel,
                    "terminal": terminal_config,
                    "min_val": min_val, "max_val": max_val})

        class _AO:
            def add_ao_voltage_chan(self, physical, **kw):
                task.ao_calls.append((physical, kw))

        class _Timing:
            samp_clk_rate = 0.0

            def cfg_samp_clk_timing(self, rate=None, sample_mode=None,
                                    samps_per_chan=None):
                if task.fail_timing:
                    raise RuntimeError("injected timing failure")
                task.timing_calls.append({
                    "rate": rate, "sample_mode": sample_mode,
                    "samps_per_chan": samps_per_chan})
                self.samp_clk_rate = rate

        class _StartTrig:
            def cfg_dig_edge_start_trig(self, src, trigger_edge=None):
                task.dig_trig_calls.append((src, trigger_edge))

            def cfg_anlg_edge_start_trig(self, src, trigger_slope=None,
                                         trigger_level=None):
                task.anlg_trig_calls.append(
                    (src, trigger_slope, trigger_level))

        class _Triggers:
            start_trigger = _StartTrig()

        self.ai_channels = _AI()
        self.ao_channels = _AO()
        self.timing = _Timing()
        self.triggers = _Triggers()

    def write(self, data, auto_start=False):
        self.writes.append((data, auto_start))

    def start(self):
        self.started = True

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


class _FakeNidaqmx:
    """Stand-in for the nidaqmx module: Task(...) factory + task log."""

    def __init__(self):
        self.tasks = []
        self.fail_timing = False

    def Task(self, name=""):
        t = _FakeTask(name, fail_timing=self.fail_timing)
        self.tasks.append(t)
        return t


class _FakeReaders:
    class AnalogMultiChannelReader:
        def __init__(self, in_stream):
            self.in_stream = in_stream

        def read_many_sample(self, data,
                             number_of_samples_per_channel=0, timeout=0.0):
            return 0                       # never any samples


class _FakeAcqType:
    CONTINUOUS = "CONTINUOUS"


class _FakeEdge:
    RISING = "EDGE_RISING"
    FALLING = "EDGE_FALLING"


class _FakeSlope:
    RISING = "SLOPE_RISING"
    FALLING = "SLOPE_FALLING"


class _FakeTC:
    DIFF = "TC_DIFF"
    RSE = "TC_RSE"
    NRSE = "TC_NRSE"


_MISSING = object()
# device.py binds these at import when nidaqmx is installed; when it is not,
# only ``nidaqmx = None`` (and a fallback DaqError) exist — so save/restore
# must handle attributes that may be absent from the module namespace.
_HW_ATTRS = ("nidaqmx", "_readers", "AcquisitionType", "Edge", "Slope", "_TC")


def _patch_fake_hw():
    fake = _FakeNidaqmx()
    values = {"nidaqmx": fake, "_readers": _FakeReaders,
              "AcquisitionType": _FakeAcqType, "Edge": _FakeEdge,
              "Slope": _FakeSlope, "_TC": _FakeTC}
    saved = {k: getattr(ni_device, k, _MISSING) for k in _HW_ATTRS}
    for k, v in values.items():
        setattr(ni_device, k, v)
    return fake, saved


def _unpatch_fake_hw(saved):
    for k, v in saved.items():
        if v is _MISSING:
            if hasattr(ni_device, k):
                delattr(ni_device, k)
        else:
            setattr(ni_device, k, v)


def _hw_config(**kw):
    cfg = NiDaqConfig(force_sim=False, scan_hz=1000.0, poll_ms=5, **kw)
    return cfg


def test_hw_channels_ranges_and_timing():
    fake, saved = _patch_fake_hw()
    try:
        dev = NiUsb6351(_hw_config())
        try:
            dev.connect()
            assert dev.connected and not dev.sim_mode
            task = fake.tasks[0]
            # every ENABLED channel added as Dev2/aiN — Spare (ai7) absent
            assert [c["physical"] for c in task.ai_calls] == \
                [f"Dev2/ai{i}" for i in range(7)]
            assert [c["name"] for c in task.ai_calls] == \
                ["N1", "N2", "Y1", "Y2", "Axial", "Roll", "Excitation"]
            # symmetric ±native_range limits, snapped from the requested span
            for call, r in zip(task.ai_calls,
                               (0.2, 0.2, 0.2, 0.2, 0.5, 0.5, 10.0)):
                assert call["min_val"] == -r and call["max_val"] == r
            assert all(c["terminal"] == _FakeTC.DIFF for c in task.ai_calls)
            # continuous hardware-timed clock at scan_hz, ≥5 s buffer
            t = task.timing_calls[0]
            assert t["rate"] == 1000.0
            assert t["sample_mode"] == _FakeAcqType.CONTINUOUS
            assert t["samps_per_chan"] == 5000
            assert dev.actual_hz == 1000.0
            # immediate trigger → NO trigger configuration at all
            assert task.dig_trig_calls == [] and task.anlg_trig_calls == []
            assert task.started
            # default config has no enabled AO → only the one AI task
            assert len(fake.tasks) == 1
        finally:
            dev.disconnect()
        assert fake.tasks[0].stopped and fake.tasks[0].closed
    finally:
        _unpatch_fake_hw(saved)


def test_hw_digital_edge_trigger():
    fake, saved = _patch_fake_hw()
    try:
        cfg = _hw_config()
        cfg.trigger.mode = "digital_edge"
        cfg.trigger.source = "PFI3"
        cfg.trigger.edge = "falling"
        dev = NiUsb6351(cfg)
        try:
            dev.connect()
            task = fake.tasks[0]
            assert task.dig_trig_calls == [("/Dev2/PFI3", _FakeEdge.FALLING)]
            assert task.anlg_trig_calls == []
            assert dev.waiting_for_trigger      # armed, no samples yet
        finally:
            dev.disconnect()
    finally:
        _unpatch_fake_hw(saved)


def test_hw_analog_edge_trigger_on_scanned_channel():
    # DAQmx wants the VIRTUAL channel name for an in-task AI source
    fake, saved = _patch_fake_hw()
    try:
        cfg = _hw_config()
        cfg.trigger.mode = "analog_edge"
        cfg.trigger.source = "ai0"
        cfg.trigger.edge = "rising"
        cfg.trigger.level_v = 0.05
        dev = NiUsb6351(cfg)
        try:
            dev.connect()
            task = fake.tasks[0]
            assert task.anlg_trig_calls == [("N1", _FakeSlope.RISING, 0.05)]
            assert task.dig_trig_calls == []
        finally:
            dev.disconnect()
    finally:
        _unpatch_fake_hw(saved)


def test_hw_analog_edge_trigger_on_apfi0():
    fake, saved = _patch_fake_hw()
    try:
        cfg = _hw_config()
        cfg.trigger.mode = "analog_edge"
        cfg.trigger.source = "APFI0"
        cfg.trigger.edge = "falling"
        cfg.trigger.level_v = 2.5
        dev = NiUsb6351(cfg)
        try:
            dev.connect()
            task = fake.tasks[0]
            assert task.anlg_trig_calls == \
                [("/Dev2/APFI0", _FakeSlope.FALLING, 2.5)]
        finally:
            dev.disconnect()
    finally:
        _unpatch_fake_hw(saved)


def test_hw_analog_edge_on_disabled_channel_raises():
    fake, saved = _patch_fake_hw()
    try:
        cfg = _hw_config()
        cfg.trigger.mode = "analog_edge"
        cfg.trigger.source = "ai15"        # not an enabled channel
        dev = NiUsb6351(cfg)
        try:
            dev.connect()
            assert False, "analog trigger on a non-scanned AI must raise"
        except RuntimeError as exc:
            assert "ai15" in str(exc)
        assert not dev.connected
        # the half-built task must not leak
        assert fake.tasks[0].closed
    finally:
        _unpatch_fake_hw(saved)


def test_hw_connect_failure_closes_task():
    fake, saved = _patch_fake_hw()
    fake.fail_timing = True                # blow up mid-setup
    try:
        dev = NiUsb6351(_hw_config())
        try:
            dev.connect()
            assert False, "injected timing failure must propagate"
        except RuntimeError as exc:
            assert "injected timing failure" in str(exc)
        assert not dev.connected
        assert fake.tasks[0].closed and not fake.tasks[0].started
        assert dev._task is None and dev._reader is None
    finally:
        _unpatch_fake_hw(saved)


# ── emulator ─────────────────────────────────────────────────────────────
def test_emulator_block():
    chans = NiDaqConfig().enabled_channels()
    core = SimCore(chans)
    block = core.block(0.0, 250, 1e-3)
    assert sorted(block) == sorted(c.name for c in chans)
    for v in block.values():
        assert isinstance(v, np.ndarray) and v.shape == (250,)
    assert abs(float(np.mean(block["Excitation"])) - 10.0) < 0.05
    # bridge channels sit inside their native range at wind-off
    for ch in chans:
        if ch.balance:
            assert float(np.max(np.abs(block[ch.name]))) < ch.native_range


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} ni6351 tests passed.")


if __name__ == "__main__":
    _run_all()
