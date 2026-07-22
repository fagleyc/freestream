"""Render the tunnel window in sim, spin the fan up, screenshot it.

    python -m tunnel_plc._gui_screenshot [out.png]
"""

from __future__ import annotations

import sys

from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication

from tunnel_plc.app.main_window import TunnelMainWindow
from tunnel_plc.config import TunnelConfig
from tunnel_plc.control import TunnelControl


def main() -> int:
    out = sys.argv[1] if len(sys.argv) > 1 else "tunnel_gui.png"
    app = QApplication([])
    cfg = TunnelConfig(force_sim=True, poll_s=0.05, rpm_max=900)
    win = TunnelMainWindow(cfg)
    win.show()
    win.panel._handle_connect()

    def run():
        win.panel.control = TunnelControl(cfg, win.panel.monitor,
                                          enable_writes=True)
        win.panel.rpm_spin.setRange(0, cfg.rpm_max)
        win.panel._set_armed_ui(True)
        win.panel.control.set_rpm(600)
        win.panel.control.start_tunnel_fan()
        win.panel.control.start_cooling_fan()

    def shoot():
        win.panel._refresh_ui()
        win.grab().save(out)
        print("saved", out, "- rpm",
              win.panel.monitor.snapshot().actual_rpm)
        win.close()
        app.quit()

    QTimer.singleShot(300, run)
    QTimer.singleShot(2600, shoot)
    QTimer.singleShot(9000, lambda: app.exit(2))
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
