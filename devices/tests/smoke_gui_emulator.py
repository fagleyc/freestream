"""Headless GUI smoke test against the FakeOGI emulator over real sockets.

    QT_QPA_PLATFORM=offscreen python tests/smoke_gui_emulator.py
Uses offset ports so it never collides with the real rig.
"""

import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtWidgets import QApplication

from ate_balance.config import AteConfig
from ate_balance.emulator import FakeOGI
from ate_balance.app.main_window import AteBalanceMainWindow

TMSC, TMSD, OGIT = 13050, 13051, 13052


def main() -> int:
    app = QApplication(sys.argv[:1])
    cfg = AteConfig(ogi_ip="127.0.0.1", tmsc_port=TMSC, tmsd_port=TMSD,
                    ogit_port=OGIT, auto_trigger=False)
    win = AteBalanceMainWindow(cfg)
    win.show()

    ogi = FakeOGI(tms_ip="127.0.0.1", tmsc_port=TMSC, tmsd_port=TMSD,
                  ogit_port=OGIT, data_rate_hz=100.0)
    panel = win.panel
    try:
        panel._handle_connect()
        assert panel.device.connected and not panel.device.sim_mode
        ogi.start()

        deadline = time.perf_counter() + 8.0
        while time.perf_counter() < deadline and \
                panel.device.frame_count() < 20:
            app.processEvents()
            time.sleep(0.01)
        assert panel.device.link_up, "emulator never dialled the GUI client"
        assert panel.device.frame_count() >= 20, "no LOADS frames via sockets"

        # let the 2 Hz GET_POSITIONS housekeeping fire and be answered
        deadline = time.perf_counter() + 3.0
        while time.perf_counter() < deadline:
            app.processEvents()
            time.sleep(0.01)
    finally:
        ogi.stop()
        win.close()
        app.processEvents()

    print(f"PASS smoke_gui_emulator: {panel.device.frame_count()} frames")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
