"""StrainBook driver tests — gain math, config round-trip, sim streaming,
tare. No DLL or hardware required.
"""

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strainbook_616 import daqx
from strainbook_616.config import StrainbookConfig
from strainbook_616.device import Strainbook616


def test_gain_picker_matches_rig_ranges():
    # ±11 mV -> ×447 (IAG 100 × PGA 4.47), the rig's N1..Y2 range
    total, iag, pga = daqx.pick_gain_for_range(11.0)
    assert (round(total, 2), iag, pga) == (447.0, 2, 6)
    assert abs(daqx.range_mv(total) - 11.186) < 0.01
    # ±32 mV -> ×155.8 (IAG 10 × PGA 15.58), the rig's Axial/Roll range
    total, iag, pga = daqx.pick_gain_for_range(32.0)
    assert (round(total, 2), iag, pga) == (155.8, 1, 11)
    # full scale
    total, iag, pga = daqx.pick_gain_for_range(5000.0)
    assert (total, iag, pga) == (1.0, 0, 0)
    # tiny range clamps to max gain ×20000
    total, _i, _p = daqx.pick_gain_for_range(0.2)
    assert total == 20000.0


def test_counts_to_volts_signed():
    # StrainBook data is SIGNED two's complement (live-verified 2026-07-16
    # against known physical inputs): 0 = 0 V, 0x7FFF = +FS, 0x8000 = -FS.
    assert abs(daqx.counts_to_volts(0, 1.0)) < 1e-6            # 0 counts=0 V
    assert abs(daqx.counts_to_volts(32767, 1.0) - 5.0) < 1e-3  # +full scale
    assert abs(daqx.counts_to_volts(32768, 1.0) + 5.0) < 1e-6  # -full scale
    assert abs(daqx.counts_to_volts(16384, 1.0) - 2.5) < 1e-3  # +half
    assert abs(daqx.counts_to_volts(49152, 1.0) + 2.5) < 1e-3  # -half
    # gain 447: the rig's real bridge counts (0xFD22 = -734) decode to the
    # microvolt level LabVIEW reports -- NOT railed (this was the bug)
    assert abs(daqx.counts_to_volts(0xFD22, 447.0) - (-250e-6)) < 5e-6
    # unipolar excitation readback: 10 V supply reads ~+4.86 V signed;
    # the driver adds ADC_FS_V to recover the true 0-10 V value
    assert abs((daqx.counts_to_volts(31868, 1.0) + daqx.ADC_FS_V)
               - 9.863) < 2e-3


def test_config_defaults_and_roundtrip():
    import tempfile
    cfg = StrainbookConfig()
    names = [c.name for c in cfg.channels]
    assert names == ["N1", "N2", "Y1", "Y2", "Axial", "Roll", "Excitation"]
    assert cfg.channels[0].gain[0] == 447.0
    assert cfg.channels[4].gain[0] == 155.8
    assert cfg.channels[6].read_excitation
    # excitation banks are gone — the rig uses an external supply
    assert not hasattr(cfg, "excitation_bank1_v")
    assert not hasattr(cfg, "excitation_bank2_v")
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "cfg.json"
        cfg.channels[0].range_mv = 32.0
        cfg.save(p)
        back = StrainbookConfig.load(p)
    assert back.channels[0].range_mv == 32.0
    assert back.channels[0].gain[0] == 155.8


def test_default_channels_scale_and_units():
    """Scale is forced to 1.0 everywhere (display is raw volts), bridge /
    Axial / Roll display in V, and CH8 is the external-excitation readback."""
    cfg = StrainbookConfig()
    assert all(c.scale == 1.0 for c in cfg.channels)
    for c in cfg.channels[:6]:                     # N1..Y2, Axial, Roll
        assert c.unit == "V" and not c.read_excitation
    exc = cfg.channels[6]
    assert exc.read_excitation and exc.offset == 0.0 and exc.unit == "V"
    assert exc.bridge == daqx.BRIDGE_FULL           # full-bridge completion
    assert exc.gain[0] == 1.0                       # ×1 for the readback


def test_excitation_dac_codes():
    assert daqx.EXC_DAC[10.0] == 0x5000
    assert daqx.EXC_DAC[0.0] == 0x0000


def test_sim_device_streams_and_tare():
    cfg = StrainbookConfig(force_sim=True, scan_hz=500.0)
    dev = Strainbook616(cfg)
    try:
        dev.connect()
        dev.start()
        deadline = time.perf_counter() + 5.0
        while time.perf_counter() < deadline and dev.frame_count() < 500:
            time.sleep(0.05)
        assert dev.frame_count() >= 500

        latest = dev.latest()
        assert latest is not None
        # scale is forced to 1.0 — the display value IS the raw volts
        assert abs(latest["N1"] - latest["N1_V"]) < 1e-9
        # excitation reads ~10 V verbatim (readback, offset 0)
        assert 9.5 < latest["Excitation"] < 10.5

        # tare pulls bridge means to ~0 without touching excitation;
        # tare_count bumps so peak-hold displays know to reset
        assert dev.tare_count == 0
        before = abs(np.mean(dev.ring.tail(200)["N1"]))
        dev.tare(seconds=0.3)
        assert dev.tare_count == 1
        time.sleep(0.5)
        after = abs(np.mean(dev.ring.tail(100)["N1"]))
        assert after < max(before, 0.05)
        assert 9.5 < dev.latest()["Excitation"] < 10.5

        dev.clear_tare()
    finally:
        dev.disconnect()
    assert not dev.connected


def test_driver_never_commands_internal_excitation():
    """The bridges run on an EXTERNAL supply — the driver must never write
    the StrainBook's internal excitation DAC or latch an excitation source.
    Drives the real hardware path against a recording fake DaqX."""
    import ctypes

    from strainbook_616 import device as sb_device

    calls = []
    scan = {}

    class FakeLib:
        def open(self, name):
            return 1

        def set_option(self, h, chan, option_type, value, flags=0):
            calls.append((int(chan), int(option_type), int(value)))

        def adc_set_scan(self, h, channels, gains, flags):
            scan["channels"] = list(channels)
            scan["flags"] = list(flags)

        def adc_set_freq(self, *a):
            pass

        def adc_get_freq(self, h):
            return 200.0

        def adc_set_acq(self, *a):
            pass

        def adc_set_trig(self, *a):
            pass

        def make_buffer(self, scans, nch):
            return (ctypes.c_uint16 * (scans * nch))()

        def transfer_set_buffer(self, *a):
            pass

        def transfer_start(self, *a):
            pass

        def arm(self, *a):
            pass

        def transfer_get_stat(self, h):
            return (0, 0)

        def disarm(self, *a):
            pass

        def transfer_stop(self, *a):
            pass

        def close(self, *a):
            pass

    orig = sb_device.daqx.DaqX
    sb_device.daqx.DaqX = lambda *a, **k: FakeLib()
    try:
        dev = Strainbook616(StrainbookConfig(force_sim=False, scan_hz=200.0))
        try:
            dev.connect()
            assert dev.connected and not dev.sim_mode
            types = [t for _c, t, _v in calls]
            assert daqx.DcotWbk16ExcDac not in types, \
                "driver wrote the internal excitation DAC"
            assert daqx.DmotWbk16Immediate not in types, \
                "driver latched an internal excitation source"
            # every channel (CH8 included) reads its INPUT signal — never
            # OUT_EXC_VOLTS, which monitors only the (off) internal banks
            src = [(c, v) for c, t, v in calls
                   if t == daqx.DcotWbk16OutSource]
            assert src and all(v == daqx.OUT_SIGNAL for _c, v in src)
            # per-channel scan polarity: bridges bipolar, CH8 unipolar
            exc_daqx_ch = 8 + daqx.STRAIN_CHANNEL_OFFSET
            for c, f in zip(scan["channels"], scan["flags"]):
                if c == exc_daqx_ch:
                    assert not (f & daqx.DafBipolar), \
                        "excitation readback must scan unipolar"
                else:
                    assert f & daqx.DafBipolar, \
                        "bridge channels must scan bipolar"
        finally:
            dev.disconnect()
    finally:
        sb_device.daqx.DaqX = orig


def test_publish_counts_shifts_unipolar_excitation():
    """The hardware publish path adds ADC_FS_V to read_excitation channels
    (unipolar scan arrives one half-span down) and leaves bridges alone —
    raw counts from the live rig must reproduce the physical values."""
    from strainbook_616.datamodel import ScanRingBuffer, fields_for

    cfg = StrainbookConfig(force_sim=True)
    dev = Strainbook616(cfg)
    dev._chans = cfg.enabled_channels()
    dev.ring = ScanRingBuffer(fields_for([c.name for c in dev._chans]))
    n = 4
    counts = np.zeros((n, len(dev._chans)))
    counts[:, 0] = 0xFD22    # N1 live raw: -734 counts = -250 uV at x447
    counts[:, 6] = 31868     # excitation live raw at a ~9.86 V supply
    dev._publish_counts(np.arange(n, dtype=float), counts)
    latest = dev.latest()
    assert abs(latest["N1"] - (-250e-6)) < 5e-6
    assert 9.85 < latest["Excitation"] < 9.88


def test_forces_panel_clear_vol_resets_overstress():
    """The Forces tab's Clear button drops the cal AND resets the
    overstress alarm; a latched alarm also DECAYS on refresh once the cal
    is gone (no stale 'balance overstressed' after clearing/taring)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    from strainbook_616.app.forces_panel import ForcesPanel

    _app = QApplication.instance() or QApplication([])
    panel = ForcesPanel(StrainbookConfig(vol_path="C:/cal/old.vol"))
    try:
        assert panel.clear_btn.text() == "Clear"
        panel.cal = object()
        panel.overstress = True
        panel.alarm.setVisible(True)
        panel.clear_vol()
        assert panel.cal is None
        assert panel.config.vol_path == ""
        assert panel.overstress is False and panel.alarm.isHidden()
        # decay path: refresh with no cal clears a latched alarm too
        panel.overstress = True
        panel.alarm.setVisible(True)
        panel.refresh(None, 200.0, 30.0)
        assert panel.overstress is False and panel.alarm.isHidden()
    finally:
        panel.deleteLater()


def test_history_channel_visibility_toggles():
    """Per-channel show/hide on the live plot: hiding a bridge hides its
    curve, hiding Excitation collapses the strip, and the choice survives
    a channel rebind (Force↔Moment / reconnect)."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    from strainbook_616.app.plots import BridgeHistory

    _app = QApplication.instance() or QApplication([])
    cfg = StrainbookConfig()
    hist = BridgeHistory()
    try:
        hist.set_channels(cfg.channels, None)
        assert hist.channel_visible("N1")
        hist.set_channel_visible("N1", False)
        assert not hist._bridge_curves["N1"].isVisible()
        assert hist._bridge_curves["N2"].isVisible()
        # hiding the only excitation curve collapses the whole strip
        hist.set_channel_visible("Excitation", False)
        assert not hist._exc_plot.isVisibleTo(hist)
        hist.set_channel_visible("Excitation", True)
        assert hist._exc_plot.isVisibleTo(hist)
        # visibility is name-keyed and survives a rebind
        hist.set_channels(cfg.channels, None)
        assert not hist._bridge_curves["N1"].isVisible()
        assert not hist.channel_visible("N1")
    finally:
        hist.deleteLater()


def test_channels_panel_range_selection_toggles_excitation():
    """The Range dropdown is the single per-channel range control: picking
    "0 to 10 V" makes a channel the external-excitation readback; picking a
    ± mV range clears it and resets the offset."""
    import os
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication

    from strainbook_616.app.channels_panel import ChannelsPanel, _EXC_RANGE

    _app = QApplication.instance() or QApplication([])
    cfg = StrainbookConfig()
    panel = ChannelsPanel(cfg)
    try:
        # a normal bridge channel → "0 to 10 V" turns it into the readback
        n1 = cfg.channels[0]
        assert not n1.read_excitation
        panel._range_changed(n1, _EXC_RANGE, 0)
        assert n1.read_excitation and n1.offset == 0.0 and n1.unit == "V"
        assert n1.range_mv == 5000.0 and n1.gain[0] == 1.0

        # the CH8 readback → any ± mV range clears read_excitation + offset
        exc = cfg.channels[6]
        assert exc.read_excitation
        panel._range_changed(exc, 11.0, 6)
        assert not exc.read_excitation and exc.offset == 0.0
        assert exc.range_mv == 11.0
    finally:
        panel.deleteLater()


def _run_all():
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} strainbook tests passed.")


if __name__ == "__main__":
    _run_all()
