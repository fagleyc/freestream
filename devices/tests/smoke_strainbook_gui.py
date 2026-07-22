"""Headless StrainBook GUI smoke test — sim mode.

    QT_QPA_PLATFORM=offscreen python tests/smoke_strainbook_gui.py
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

from strainbook_616.config import StrainbookConfig
from strainbook_616.app.main_window import StrainbookMainWindow


def main() -> int:
    app = QApplication(sys.argv[:1])
    # the sim models the rig's external excitation supply at 10 V, so the
    # forces pipeline normalizes sensibly in sim
    cfg = StrainbookConfig(force_sim=True, scan_hz=500.0)
    win = StrainbookMainWindow(cfg)
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

    assert panel.tiles._tiles["N1"].value.text() != "--"
    xs, _ys = panel.history._bridge_curves["Axial"].getData()
    assert xs is not None and len(xs) >= 200, "bridge history empty"
    xe, _ye = panel.history._exc_curves["Excitation"].getData()
    assert xe is not None and len(xe) >= 200, "excitation history empty"

    # tare via the GUI button
    panel.tare_btn.click()
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 0.5:
        app.processEvents()
        time.sleep(0.01)
    import numpy as np
    n1 = panel.device.ring.tail(100)["N1"]
    assert abs(float(np.mean(n1))) < 0.05, "tare did not zero N1"

    # .vol calibration -> live forces
    vol = (Path(__file__).resolve().parents[2] / "Streamlined" /
           "CalFiles" / "2025_06_06_2 100 lb.vol")
    if vol.exists():
        assert panel.forces_panel.load_vol(str(vol)), "vol load failed"
        assert panel.config.balance_serial, "balance metadata not stored"
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 0.5:
            app.processEvents()
            time.sleep(0.01)
        panel.tabs.setCurrentIndex(1)          # Forces tab visible
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 0.5:
            app.processEvents()
            time.sleep(0.01)
        assert panel.forces_panel.tiles["Fz"].value.text() != "--", \
            "force tiles never updated"
        xs_f, _ys = panel.forces_panel._curves["Fz"].getData()
        assert xs_f is not None and len(xs_f) > 20, "forces history empty"
        assert not panel.forces_panel.overstress, \
            "sim signals should not overstress"
        panel.tabs.setCurrentIndex(0)
    else:
        print("  (vol smoke skipped — CalFiles not present)")

    # interactive plot: follow re-pin works
    panel.history.follow = False
    panel.follow_btn.click()
    assert panel.history.follow is True

    # settings dialog round-trip
    from strainbook_616.app.settings_dialog import SettingsDialog
    dlg = SettingsDialog(panel.config, win)
    dlg.tile_avg.setValue(400)
    dlg.accept()
    panel.apply_settings()
    assert panel.tiles.avg_ms == 400

    win.close()
    app.processEvents()
    print(f"PASS smoke_strainbook_gui: {panel.device.frame_count()} scans")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
