"""Render the traverse window in sim mid-move, screenshot it.

    python -m traverse_swt._gui_screenshot [out.png]
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from traverse_swt.app.main_window import TraverseMainWindow
from traverse_swt.config import TraverseConfig


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "traverse_gui.png"
    app = QApplication([])
    cfg = TraverseConfig(force_sim=True, loop_ms=20)
    # calibrated so the axis cards show inches + the STOP is live
    for ax, slope in ((cfg.x, 13705.6), (cfg.y, -14841.0), (cfg.z, -14841.0)):
        ax.clicks_per_inch = slope
        ax.inch_high, ax.counts_high, ax.calibrated = 0.0, 0, True
        ax.min_in, ax.max_in = -6.0, 6.0
    win = TraverseMainWindow(cfg)
    win.show()
    win.panel._handle_connect()

    def run():
        win.panel.device.move_to(x=3.0, y=-2.0)    # multi-axis move

    def finish():
        win.panel._refresh_ui()
        st = win.panel.device.state()
        win.grab().save(out)
        print("saved", out, "- X", f"{st['X']['inches']:+.3f}in",
              "Y", f"{st['Y']['inches']:+.3f}in")
        win.close()
        app.quit()

    QTimer.singleShot(300, run)
    QTimer.singleShot(1500, finish)         # capture mid-move (STOP live)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
