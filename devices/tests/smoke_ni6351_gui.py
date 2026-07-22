"""Headless NI USB-6351 GUI smoke test — sim mode.

    QT_QPA_PLATFORM=offscreen python tests/smoke_ni6351_gui.py

Also collectable by pytest via :func:`test_smoke_ni6351_gui`.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

from ni_usb_6351.config import ANALOG_SOURCES, PFI_SOURCES, NiDaqConfig
from ni_usb_6351.app.main_window import NiDaqMainWindow


def _pump(app, seconds: float) -> None:
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < seconds:
        app.processEvents()
        time.sleep(0.01)


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    cfg = NiDaqConfig(force_sim=True, scan_hz=500.0)
    cfg.ao_channels[0].enabled = True          # exercise AO in sim
    win = NiDaqMainWindow(cfg)
    win.show()
    panel = win.panel

    panel._handle_connect()
    assert panel.device.connected and panel.device.sim_mode

    deadline = time.perf_counter() + 6.0
    while time.perf_counter() < deadline and panel.device.frame_count() < 200:
        app.processEvents()
        time.sleep(0.01)
    assert panel.device.frame_count() >= 200, "no scans in sim mode"

    _pump(app, 0.4)

    assert panel.tiles._tiles["N1"].value.text() != "--"
    xs, _ys = panel.history._curves["N1"].getData()
    assert xs is not None and len(xs) >= 100, "channel history empty"
    xe, _ye = panel.history._curves["Excitation"].getData()
    assert xe is not None and len(xe) >= 100, "excitation trace empty"

    # tare via the GUI button
    panel.tare_btn.click()
    _pump(app, 0.5)
    import numpy as np
    n1 = panel.device.ring.tail(100)["N1"]
    assert abs(float(np.mean(n1))) < 0.05, "tare did not zero N1"

    # balance-layout switch renames the four bridge channels live
    panel.forces_panel.bal_config.setCurrentText("Moment")
    _pump(app, 0.3)
    assert panel.config.balance_config == "Moment"
    assert any(c.name == "AftPitch" for c in panel.config.channels), \
        "bridge channels not renamed"
    assert "AftPitch_V" in panel.device.ring.fields, "ring not renamed"
    assert "AftPitch" in panel.history._curves, "history not rebound"
    panel.forces_panel.bal_config.setCurrentText("Force")
    _pump(app, 0.3)
    assert any(c.name == "N1" for c in panel.config.channels)

    # trigger-mode combo repopulates sources and mutates cfg.trigger
    op = panel.output_panel
    op.mode_combo.setCurrentText("digital_edge")
    assert panel.config.trigger.mode == "digital_edge"
    assert op.source_combo.count() == len(PFI_SOURCES)
    assert not op.level_spin.isEnabled()
    op.mode_combo.setCurrentText("analog_edge")
    assert op.source_combo.count() == len(ANALOG_SOURCES)
    assert op.level_spin.isEnabled()
    _pump(app, 0.2)                     # state lamp tick (sim: acquiring)
    op.mode_combo.setCurrentText("immediate")
    _pump(app, 0.2)
    assert op.state_lamp.text() == "Immediate"

    # AO static set in sim (stored in cfg, driver status only)
    row = op._ao_widgets[0]
    row["static"].setValue(1.5)
    row["set"].click()
    assert abs(panel.config.ao_channels[0].static_v - 1.5) < 1e-9
    op.start_wave_btn.click()           # sim: no-op with status
    op.zero_btn.click()
    assert panel.config.ao_channels[0].static_v == 0.0

    # interactive plot: follow re-pin works
    panel.history.follow = False
    panel.follow_btn.click()
    assert panel.history.follow is True

    # settings dialog round-trip
    from ni_usb_6351.app.settings_dialog import SettingsDialog
    dlg = SettingsDialog(panel.config, win)
    dlg.tile_avg.setValue(400)
    dlg.accept()
    panel.apply_settings()
    assert panel.tiles.avg_ms == 400

    # save / load config round trip
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "ni6351_config.json"
        panel.config.save(path)
        loaded = NiDaqConfig.load(path)
        assert loaded.scan_hz == panel.config.scan_hz
        assert loaded.tile_avg_ms == 400
        assert loaded.trigger.mode == panel.config.trigger.mode
        assert loaded.ao_channels[0].enabled is True
        assert len(loaded.channels) == len(panel.config.channels)
        assert [c.name for c in loaded.channels] == \
            [c.name for c in panel.config.channels]

    win.close()
    app.processEvents()
    print(f"PASS smoke_ni6351_gui: {panel.device.frame_count()} scans")
    return 0


def test_smoke_ni6351_gui():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
