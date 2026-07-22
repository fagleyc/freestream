"""Headless ARC Crescent GUI smoke test — sim physics.

    QT_QPA_PLATFORM=offscreen python tests/smoke_crescent_gui.py
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

from ac_delta.config import CrescentConfig
from ac_delta.app.main_window import CrescentMainWindow


def main() -> int:
    app = QApplication(sys.argv[:1])
    cfg = CrescentConfig(force_sim=True, loop_ms=20)
    for ax in cfg.axes():
        ax.calibrated = True
        ax.clicks_per_degree = 100.0
        ax.angle_high = 20.0
        ax.encoder_high = 2000
    win = CrescentMainWindow(cfg)
    win.show()
    panel = win.panel

    panel._handle_connect()
    assert panel.device.connected and panel.device.sim_mode

    # synchronous move through the GUI
    panel.sync_alpha.setValue(3.0)
    panel.sync_beta.setValue(-2.0)
    panel.sync_btn.click()

    deadline = time.perf_counter() + 30.0
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.01)
        st = panel.device.state()
        if not st["Alpha"]["moving"] and not st["Beta"]["moving"] and \
                time.perf_counter() - deadline < -1.0:
            if abs(st["Alpha"]["angle"] - 3.0) < 0.3:
                break
    st = panel.device.state()
    assert abs(st["Alpha"]["angle"] - 3.0) < 0.3, "alpha did not reach target"
    assert abs(st["Beta"]["angle"] + 2.0) < 0.3, "beta did not reach target"

    # angle cards updated (big readout shows angle — axes are calibrated)
    assert panel.alpha_card.big_lbl.text() != "--"
    assert panel.alpha_card.unit_lbl.text() == "deg"
    # plot got data
    xs, _ys = panel._curves["Alpha"].getData()
    assert xs is not None and len(xs) > 20, "angle history empty"

    # E-stop path
    panel.estop_btn.click()
    app.processEvents()

    # hold-to-run jog via the card buttons (pressed → released)
    a0 = panel.device.state()["Alpha"]["angle"]
    panel.alpha_card.jog_plus.pressed.emit()
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < 0.6:
        app.processEvents()
        time.sleep(0.01)
    assert panel.device.state()["Alpha"]["jogging"], "jog did not engage"
    panel.alpha_card.jog_plus.released.emit()
    app.processEvents()
    assert not panel.device.state()["Alpha"]["jogging"], "jog did not stop"
    assert panel.device.state()["Alpha"]["angle"] > a0, "jog did not move"

    # settings round-trip
    from ac_delta.app.settings_dialog import SettingsDialog
    dlg = SettingsDialog(panel.config, win)
    dlg.loop_ms.setValue(30)
    dlg.accept()
    assert panel.config.loop_ms == 30

    # config reload must rebuild the calibration page without the
    # "already has a layout" error (regression: loading a JSON config)
    new_cfg = CrescentConfig(force_sim=True)
    panel.config = new_cfg
    panel.device.config = new_cfg
    panel.cal_panel.set_config(new_cfg)
    panel.cal_panel.set_config(CrescentConfig(force_sim=True))  # twice
    app.processEvents()
    assert panel.cal_panel.alpha_cal is not None
    assert panel.cal_panel.layout() is not None
    panel.cal_panel.refresh({"Alpha": {"encoder": 5},
                             "Beta": {"encoder": -5}})
    assert panel.cal_panel.alpha_cal.live_enc.text() == "+5"

    win.close()
    app.processEvents()
    print(f"PASS smoke_crescent_gui: alpha {st['Alpha']['angle']:+.2f}, "
          f"beta {st['Beta']['angle']:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
