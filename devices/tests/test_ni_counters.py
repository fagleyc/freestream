"""NI USB-6351 counters and pulse trains — config, driver, panel.

The 6351 has four 32-bit counters. Counter INPUTS (frequency / edge
count) stream into the same ring/blocks as the AI channels so the
recorder and the freestream adapter treat them as ordinary channels;
counter OUTPUTS are pulse trains that never start as a side effect of
Connect. Everything here runs in sim.
"""

import os
import sys
import time
from pathlib import Path

import numpy as np
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ni_usb_6351.config import (CTR_DEFAULT_OUT, CTR_DEFAULT_SRC,  # noqa: E402
                                CounterInConfig, NiDaqConfig,
                                PulseTrainConfig)
from ni_usb_6351.device import NiUsb6351                           # noqa: E402


def _cfg(**kw) -> NiDaqConfig:
    kw.setdefault("force_sim", True)
    kw.setdefault("scan_hz", 200.0)
    kw.setdefault("poll_ms", 20)
    cfg = NiDaqConfig(**kw)
    return cfg


def _dev(cfg=None) -> NiUsb6351:
    d = NiUsb6351(cfg or _cfg())
    d.connect()
    d.start()
    return d


# ── config ──────────────────────────────────────────────────────────────
def test_defaults_ship_disabled_with_rig_ready_examples():
    """The stock config shows HOW without activating anything: an RPM
    pickup (frequency, scale 60) and a sync train, both disabled."""
    cfg = NiDaqConfig()
    assert len(cfg.ci_channels) == 1 and not cfg.ci_channels[0].enabled
    assert cfg.ci_channels[0].mode == "frequency"
    assert cfg.ci_channels[0].scale == 60.0        # 1 pulse/rev → RPM
    assert len(cfg.co_channels) == 1 and not cfg.co_channels[0].enabled
    assert not cfg.enabled_ci_channels() and not cfg.enabled_co_channels()


def test_default_terminals_follow_the_x_series_pinout():
    """Blank terminal = the counter's own SRC/OUT pin, so the operator
    only touches routing when the wiring is nonstandard."""
    assert CTR_DEFAULT_SRC == {0: "PFI8", 1: "PFI3", 2: "PFI0", 3: "PFI5"}
    assert CTR_DEFAULT_OUT == {0: "PFI12", 1: "PFI13", 2: "PFI14",
                               3: "PFI15"}
    ci = CounterInConfig(ctr=2)
    assert ci.source_terminal == "PFI0"
    ci.terminal = "PFI6"
    assert ci.source_terminal == "PFI6"
    co = PulseTrainConfig(ctr=3)
    assert co.out_terminal == "PFI15"


def test_counter_config_round_trips_through_json(tmp_path):
    cfg = _cfg()
    cfg.ci_channels[0].enabled = True
    cfg.ci_channels[0].max_hz = 2000.0
    cfg.co_channels[0].enabled = True
    cfg.co_channels[0].n_pulses = 50
    cfg.co_channels[0].sync_to_ai_start = True
    path = tmp_path / "ni.json"
    cfg.save(path)
    loaded = NiDaqConfig.load(path)
    assert loaded.ci_channels[0].enabled
    assert loaded.ci_channels[0].max_hz == 2000.0
    assert loaded.co_channels[0].n_pulses == 50
    assert loaded.co_channels[0].sync_to_ai_start


def test_legacy_configs_without_counter_fields_still_load():
    """A saved bundle from before counters existed must load and get
    the (disabled) defaults."""
    cfg = NiDaqConfig()
    d = cfg.to_dict()
    d.pop("ci_channels")
    d.pop("co_channels")
    d.pop("ci_stale_s")
    loaded = NiDaqConfig.from_dict(d)
    assert loaded.ci_channels and not loaded.ci_channels[0].enabled
    assert loaded.co_channels


# ── counter input streaming ─────────────────────────────────────────────
def test_ci_channel_streams_into_the_ring_like_any_other():
    cfg = _cfg()
    cfg.ci_channels[0].enabled = True
    d = _dev(cfg)
    try:
        time.sleep(0.5)
        tail = d.ring.tail(50)
        assert "RPM" in tail and "RPM_V" in tail
        assert tail["RPM"].size > 0
        # sim frequency wanders around 500 Hz; scale 60 → ~30 kRPM
        assert 20_000 < tail["RPM"][-1] < 40_000
        # engineering IS the record for counters: both fields agree
        assert np.allclose(tail["RPM"], tail["RPM_V"])
    finally:
        d.disconnect()


def test_ci_blocks_reach_on_block_alongside_the_ai_channels():
    cfg = _cfg()
    cfg.ci_channels[0].enabled = True
    d = NiUsb6351(cfg)
    blocks = []
    d.on_block = blocks.append
    d.connect()
    d.start()
    try:
        deadline = time.monotonic() + 3.0
        while not blocks and time.monotonic() < deadline:
            time.sleep(0.02)
        assert blocks, "no blocks delivered"
        blk = blocks[-1]
        n = blk["t"].size
        assert blk["RPM"].size == n            # same length as AI columns
        assert "N1" in blk                     # AI still present
    finally:
        d.disconnect()


def test_edge_count_mode_integrates_and_resets():
    cfg = _cfg()
    cfg.ci_channels[0].enabled = True
    cfg.ci_channels[0].mode = "edge_count"
    cfg.ci_channels[0].scale = 1.0
    cfg.ci_channels[0].unit = "counts"
    d = _dev(cfg)
    try:
        time.sleep(0.4)
        first = d.ci_values()["RPM"]
        time.sleep(0.4)
        second = d.ci_values()["RPM"]
        assert second > first > 0              # monotonically counting
        d.reset_counter("RPM")
        assert d.ci_values()["RPM"] == 0.0
    finally:
        d.disconnect()


def test_reset_counter_refuses_frequency_channels():
    cfg = _cfg()
    cfg.ci_channels[0].enabled = True          # mode = frequency
    d = _dev(cfg)
    try:
        with pytest.raises(ValueError, match="edge_count"):
            d.reset_counter("RPM")
    finally:
        d.disconnect()


def test_ci_name_colliding_with_an_ai_channel_is_refused():
    """One ring, one namespace — a counter named like a bridge would
    silently clobber the recorded volts."""
    cfg = _cfg()
    cfg.ci_channels[0].enabled = True
    cfg.ci_channels[0].name = "N1"
    d = NiUsb6351(cfg)
    with pytest.raises(RuntimeError, match="collide"):
        d.connect()


# ── pulse trains ────────────────────────────────────────────────────────
def test_pulse_never_starts_as_a_side_effect_of_connect():
    cfg = _cfg()
    cfg.co_channels[0].enabled = True
    d = _dev(cfg)
    try:
        assert not d.pulse_running("Sync")
        d.start_pulse("Sync")
        assert d.pulse_running("Sync")
    finally:
        d.disconnect()
    assert not d.pulse_running("Sync")         # disconnect cleans up


def test_pulse_stop_start_and_retune():
    cfg = _cfg()
    cfg.co_channels[0].enabled = True
    d = _dev(cfg)
    try:
        d.start_pulse("Sync")
        d.set_pulse("Sync", freq_hz=250.0, duty=0.25)
        assert cfg.co_channels[0].freq_hz == 250.0
        assert cfg.co_channels[0].duty == 0.25
        assert d.pulse_running("Sync")         # retune keeps it running
        d.stop_pulse("Sync")
        assert not d.pulse_running("Sync")
        d.set_pulse("Sync", freq_hz=500.0)     # retune while stopped
        assert not d.pulse_running("Sync")     # ...does not start it
    finally:
        d.disconnect()


def test_pulse_rejects_nonsense():
    cfg = _cfg()
    cfg.co_channels[0].enabled = True
    d = _dev(cfg)
    try:
        with pytest.raises(ValueError, match="duty"):
            d.set_pulse("Sync", duty=1.5)
        with pytest.raises(ValueError, match="frequency"):
            d.set_pulse("Sync", freq_hz=-1.0)
        with pytest.raises(ValueError, match="no enabled pulse train"):
            d.start_pulse("nope")
        cfg.co_channels[0].duty = 0.0          # corrupted config
        with pytest.raises(ValueError, match="duty"):
            d.start_pulse("Sync")
    finally:
        d.disconnect()


def test_stop_all_pulses():
    cfg = _cfg()
    cfg.co_channels[0].enabled = True
    cfg.co_channels.append(PulseTrainConfig(ctr=2, name="Sync2",
                                            enabled=True))
    d = _dev(cfg)
    try:
        d.start_pulse("Sync")
        d.start_pulse("Sync2")
        d.stop_all_pulses()
        assert not d.pulse_running("Sync")
        assert not d.pulse_running("Sync2")
    finally:
        d.disconnect()


# ── freestream adapter ──────────────────────────────────────────────────
@pytest.fixture()
def adapter():
    fs = Path(__file__).resolve().parents[2]
    if str(fs) not in sys.path:
        sys.path.insert(0, str(fs))
    from freestream.adapters.ni_daq import NiDaqAdapter
    a = NiDaqAdapter(sim=True)
    a._cfg.scan_hz = 200.0
    a._cfg.ci_channels[0].enabled = True
    a._cfg.co_channels[0].enabled = True
    yield a
    if a.connected:
        a.disconnect()


def test_adapter_exposes_ci_as_an_ordinary_channel(adapter):
    adapter.connect()
    adapter.start()
    time.sleep(0.5)
    specs = {c.name: c.unit for c in adapter.channels()}
    assert specs.get("RPM") == "RPM"           # declared unit = recorded
    blk = adapter.drain_block()
    assert blk["RPM"].size > 0
    assert 20_000 < blk["RPM"][-1] < 40_000
    assert adapter.latest()["RPM"] > 0


def test_adapter_pulse_passthrough(adapter):
    adapter.connect()
    adapter.start_pulse("Sync")
    assert adapter.pulse_running("Sync")
    adapter.set_pulse("Sync", freq_hz=42.0)
    adapter.stop_all_pulses()
    assert not adapter.pulse_running("Sync")


def test_estop_kills_running_pulse_trains(adapter):
    """A pulse train may be driving external gear (strobe, PIV) — it
    must not keep firing through an emergency stop."""
    fs = Path(__file__).resolve().parents[2]
    if str(fs) not in sys.path:
        sys.path.insert(0, str(fs))
    adapter.connect()
    adapter.start_pulse("Sync")
    assert adapter.pulse_running("Sync")

    class _Mgr:                                # minimal manager stand-in
        pass

    from freestream.manager import DeviceManager
    mgr = DeviceManager.__new__(DeviceManager)
    mgr.devices = {"ni": adapter}
    mgr.roles = {}
    mgr.estop_all()
    assert not adapter.pulse_running("Sync")


# ── GUI panel ───────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def app():
    from PyQt6.QtWidgets import QApplication
    return QApplication.instance() or QApplication([sys.argv[0]])


def test_panel_live_readout_and_pulse_buttons(app):
    from ni_usb_6351.app.output_trigger_panel import OutputTriggerPanel

    cfg = _cfg()
    cfg.ci_channels[0].enabled = True
    cfg.co_channels[0].enabled = True
    dev = NiUsb6351(cfg)
    panel = OutputTriggerPanel(cfg, dev)
    try:
        dev.connect()
        dev.start()
        time.sleep(0.4)
        panel.refresh()
        live = panel._ci_widgets[0]["live"].text()
        assert "RPM" in live and live != "--"

        row = panel._co_widgets[0]
        assert row["btn"].text() == "Start"
        panel._toggle_pulse(cfg.co_channels[0])
        panel.refresh()
        assert row["btn"].text() == "Stop"
        assert row["state"].text() == "PULSING"
        panel._toggle_pulse(cfg.co_channels[0])
        panel.refresh()
        assert row["state"].text() == "idle"
    finally:
        dev.disconnect()
        panel.deleteLater()
    app.processEvents()


def test_panel_refuses_pulse_when_disconnected(app):
    from ni_usb_6351.app.output_trigger_panel import OutputTriggerPanel

    cfg = _cfg()
    cfg.co_channels[0].enabled = True
    dev = NiUsb6351(cfg)
    panel = OutputTriggerPanel(cfg, dev)
    msgs = []
    panel.statusSignal.connect(msgs.append)
    try:
        panel._toggle_pulse(cfg.co_channels[0])
        assert any("Connect" in m for m in msgs), msgs
        assert not dev.pulse_running("Sync")
    finally:
        panel.deleteLater()
    app.processEvents()
