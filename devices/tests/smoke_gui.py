"""Headless GUI smoke test — launches the app in simulation mode, connects,
lets frames flow, exercises a dwell, and shuts down.

    QT_QPA_PLATFORM=offscreen python tests/smoke_gui.py
(``QT_QPA_PLATFORM`` is set automatically if missing.)
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

from ate_balance.config import AteConfig
from ate_balance.app.main_window import AteBalanceMainWindow


def main() -> int:
    app = QApplication(sys.argv[:1])
    cfg = AteConfig(force_sim=True)
    win = AteBalanceMainWindow(cfg)
    win.show()

    panel = win.panel
    panel._handle_connect()
    assert panel.device.connected and panel.device.sim_mode

    deadline = time.perf_counter() + 5.0
    while time.perf_counter() < deadline and panel.device.frame_count() < 20:
        app.processEvents()
        time.sleep(0.01)
    assert panel.device.frame_count() >= 20, "no frames flowed in sim mode"
    assert panel._latest is not None, "UI never received a MasterFrame"

    # let the UI timer tick once more so plots catch up with the stream
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 0.3:
        app.processEvents()
        time.sleep(0.01)

    # live bar graph + run-tab time history got data
    forces_bars = panel.live_panel.bars._forces._bars
    assert any(abs(h) > 0 for h in forces_bars.opts["height"]), \
        "bar graph never updated"
    hist_curve = panel.run_panel.history._curves["Lift"]
    xs, _ys = hist_curve.getData()
    assert xs is not None and len(xs) >= 20, "time history has no samples"

    # settings dialog round-trips into config
    from ate_balance.app.settings_dialog import SettingsDialog
    dlg = SettingsDialog(panel.config, win)
    dlg.bar_avg.setValue(120)
    dlg.accept()
    assert panel.config.bar_avg_ms == 120
    win._apply_settings()
    assert panel.live_panel.avg_ms == 120

    # dwell round-trip
    panel._begin_dwell(alpha=2.0, beta=0.0)
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 0.5:
        app.processEvents()
        time.sleep(0.01)
    points = []
    panel.pointSignal.connect(points.append)
    panel._end_dwell()
    app.processEvents()
    assert points and points[0].n_samples > 0, "dwell produced no ReducedPoint"

    win.close()
    app.processEvents()
    print(f"PASS smoke_gui: {panel.device.frame_count()} frames, "
          f"dwell n={points[0].n_samples}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
