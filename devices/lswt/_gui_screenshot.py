"""Render the LSWT fan window in sim mid-ramp, screenshot it.

    python -m lswt._gui_screenshot [out.png]
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from lswt.app.main_window import LswtMainWindow
from lswt.config import LswtConfig


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "lswt_gui.png"
    app = QApplication([])
    cfg = LswtConfig.for_tunnel("north", force_sim=True, poll_s=0.05,
                                ramp_hz_per_s=20.0, sim_tau_s=0.6)
    win = LswtMainWindow(cfg)
    win.show()
    win.panel._handle_connect()
    win.panel.arm_btn.setChecked(True)
    win.panel._handle_arm()            # sim: no confirm dialog

    def run():
        win.panel.hz_spin.setValue(30.0)
        win.panel._start_fan()

    def finish():
        win.panel._refresh_ui()
        st = win.panel.device.state()
        win.grab().save(out)
        print("saved", out, "- actual", f"{st['actual_hz']:.1f} Hz",
              "cmd", f"{st['cmd_hz']:.1f} Hz",
              "vel", f"{st['velocity_fps']:.1f} ft/s")
        win.close()
        app.quit()

    QTimer.singleShot(300, run)
    QTimer.singleShot(2600, finish)     # capture mid/late ramp
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
