"""Headless DaqBook GUI smoke test — sim mode, tiles + history + settings.

    QT_QPA_PLATFORM=offscreen python tests/smoke_daqbook_gui.py
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

from daqbook_2000.config import DaqbookConfig
from daqbook_2000.app.main_window import DaqbookMainWindow


def main() -> int:
    app = QApplication(sys.argv[:1])
    cfg = DaqbookConfig(force_sim=True, scan_hz=500.0)
    win = DaqbookMainWindow(cfg)
    win.show()
    panel = win.panel

    panel._handle_connect()
    assert panel.device.connected and panel.device.sim_mode

    deadline = time.perf_counter() + 6.0
    while time.perf_counter() < deadline and panel.device.frame_count() < 500:
        app.processEvents()
        time.sleep(0.01)
    assert panel.device.frame_count() >= 500, "no scans in sim mode"

    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 0.4:
        app.processEvents()
        time.sleep(0.01)

    # tiles got values
    tile = panel.tiles._tiles["Pdiff"]
    assert tile.value.text() != "--", "Pdiff tile never updated"

    # history curves got full-rate data
    xs, ys = panel.history._curves["Ptot"].getData()
    assert xs is not None and len(xs) >= 400, "history has too few samples"

    # volts toggle switches the plotted field
    panel.volts_check.setChecked(True)
    panel.history.refresh()
    _xs, ys_v = panel.history._curves["Ptot"].getData()
    assert abs(float(ys_v[-1])) < 11.0, "volts trace out of ADC range"

    # settings dialog round-trip
    from daqbook_2000.app.settings_dialog import SettingsDialog
    dlg = SettingsDialog(panel.config, win)
    dlg.tile_avg.setValue(500)
    dlg.accept()
    win.panel.apply_settings()
    assert panel.tiles.avg_ms == 500

    win.close()
    app.processEvents()
    print(f"PASS smoke_daqbook_gui: {panel.device.frame_count()} scans, "
          f"{len(xs)} plotted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
