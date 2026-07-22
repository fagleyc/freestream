"""Headless LSWT sting GUI smoke test — wire-level emulator.

    QT_QPA_PLATFORM=offscreen python tests/smoke_sting_gui.py

Also runs under pytest:  pytest tests/smoke_sting_gui.py
"""

import os
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

from lswt_sting.config import StingConfig
from lswt_sting.app.main_window import StingMainWindow


def _pump(app, seconds: float) -> None:
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.01)


def _pump_until(app, cond, timeout: float, what: str) -> None:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        app.processEvents()
        time.sleep(0.01)
        if cond():
            return
    raise AssertionError(f"timeout waiting for {what}")


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv[:1])
    import tempfile
    from pathlib import Path as _P
    cfg = StingConfig(force_sim=True, poll_ms=30,
                      park_on_disconnect=False, restore_position=False,
                      state_path=str(_P(tempfile.gettempdir())
                                     / "sting_state_guitest.json"))
    win = StingMainWindow(cfg)
    win.show()
    panel = win.panel

    # ── sim connect through the panel ──
    panel.sim.setChecked(True)
    panel._handle_connect()
    assert panel.device.connected and panel.device.sim_mode
    assert panel.stop_all_btn.isEnabled(), "STOP ALL not live"

    # absolute moves must be locked out until zeroed
    _pump(app, 0.25)
    assert not panel.beta_box.go_btn.isEnabled(), \
        "Go enabled before zeroing"
    assert "not zeroed" in panel.beta_box.big_lbl.text()

    # ── zero both axes (Set Current Angle path) ──
    panel._zero_axis("Alpha", 0.0)
    panel._zero_axis("Beta", 0.0)
    st = panel.device.state()
    assert st["Alpha"]["zeroed"] and st["Beta"]["zeroed"]
    assert panel.beta_box.go_btn.isEnabled(), "Go still locked out"
    assert panel.both_btn.isEnabled(), "Go Both still locked out"

    # ── small beta move through the GUI path ──
    win.panel.tabs.setCurrentIndex(1)          # History tab → plot live
    panel.beta_box.target.setValue(2.0)
    panel.beta_box.go_btn.click()
    _pump_until(app, lambda: panel.device.state()["Beta"]["moving"],
                2.0, "beta move start")
    _pump_until(app, lambda: not panel.device.state()["Beta"]["moving"],
                15.0, "beta move complete")
    st = panel.device.state()
    assert abs(st["Beta"]["angle"] - 2.0) < 0.1, \
        f"beta did not reach target ({st['Beta']['angle']:+.3f})"

    # readouts updated
    _pump(app, 0.25)
    assert "+" in panel.beta_box.big_lbl.text(), "beta readout stale"
    assert "steps" in panel.beta_box.sub_lbl.text()
    xs, _ys = panel._curves["Beta"].getData()
    assert xs is not None and len(xs) > 5, "angle history empty"

    # ── step jog through the GUI (move_by path) ──
    panel.beta_box.step_size.setValue(0.5)
    panel.beta_box.step_minus.click()
    _pump_until(app, lambda: not panel.device.state()["Beta"]["moving"],
                10.0, "beta step complete")
    st = panel.device.state()
    assert abs(st["Beta"]["angle"] - 1.5) < 0.1, "step − did not move"

    # ── STOP ALL mid-move (no confirmation, direct stop_all) ──
    panel.alpha_box.target.setValue(5.0)
    panel.alpha_box.go_btn.click()
    _pump_until(app, lambda: panel.device.state()["Alpha"]["moving"],
                2.0, "alpha move start")
    panel.stop_all_btn.click()
    app.processEvents()
    assert not panel.device.moving, "STOP ALL did not stop motion"
    assert panel.device.fault is None

    # ── out-of-limits move must be caught, not crash ──
    panel._move(beta=999.0)
    assert panel.device.fault is None
    assert not panel.device.state()["Beta"]["moving"]

    # ── fault injection: stall on Beta mid-move ──
    panel.device._proto._sp.inject_stall = "2"
    panel.beta_box.target.setValue(-2.0)
    panel.beta_box.go_btn.click()
    _pump_until(app, lambda: panel.device.fault is not None,
                5.0, "stall fault latch")
    _pump(app, 0.3)                            # let the UI timer catch up
    assert "STALL" in panel.device.fault
    assert "STALL" in panel.fault_lbl.toolTip(), "FAULT tooltip missing"
    assert panel.reset_fault_btn.isEnabled(), "Reset Fault not enabled"
    assert not panel.beta_box.go_btn.isEnabled(), \
        "motion enabled during fault"
    assert not panel.beta_box.step_plus.isEnabled(), \
        "jog enabled during fault"
    assert panel.stop_all_btn.isEnabled(), "STOP ALL locked during fault"

    # reset via the button path
    panel.device._proto._sp.inject_stall = None
    panel.reset_fault_btn.click()
    _pump(app, 0.3)
    assert panel.device.fault is None, "fault did not reset"
    assert not panel.reset_fault_btn.isEnabled()
    assert panel.beta_box.step_plus.isEnabled(), "jog still locked"

    # ── settings dialog construct/accept ──
    from lswt_sting.app.settings_dialog import SettingsDialog
    dlg = SettingsDialog(panel.config, win)
    dlg.poll_ms.setValue(50)
    dlg.a_vel.setText(".100")
    dlg.accept()
    panel.apply_settings()             # as File → Settings… does on OK
    assert panel.config.poll_ms == 50
    assert panel.config.alpha.velocity == ".100"
    assert panel.poll_ms.value() == 50, "Limits tab not synced"

    # ── config save/load round trip through the window path ──
    panel.a_min.setValue(-12.0)                # Limits tab edit → cfg
    assert panel.config.alpha.min_deg == -12.0
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sting_config.json"
        panel.config.save(path)
        loaded = StingConfig.load(path)
        assert loaded.alpha.min_deg == -12.0
        assert loaded.poll_ms == 50
        loaded.beta.max_deg = 10.0
        win.apply_config(loaded)
    assert panel.config.beta.max_deg == 10.0
    assert panel.b_max.value() == 10.0
    assert panel.device.config.beta.max_deg == 10.0

    st = panel.device.state()
    win.close()
    app.processEvents()
    print(f"PASS smoke_sting_gui: alpha {st['Alpha']['angle']:+.3f}, "
          f"beta {st['Beta']['angle']:+.3f}")
    return 0


def test_smoke_sting_gui():
    assert main() == 0


if __name__ == "__main__":
    raise SystemExit(main())
