"""Offscreen screenshot helper: fakes + connect + short sweep → PNG.

Usage: ``python -m freestream.app._screenshot [out.png]``
"""

from __future__ import annotations

import os
import sys
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication          # noqa: E402

from .. import theme                              # noqa: E402
from ..config import FreestreamConfig              # noqa: E402
from .main_window import FreestreamMainWindow      # noqa: E402


def main(out_path: str) -> None:
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setStyleSheet(theme.get_stylesheet())
    theme.apply_pyqtgraph_theme()

    config = FreestreamConfig(operator="screenshot", config_name="demo",
                             data_root=os.path.join(
                                 os.environ.get("TEMP", "."),
                                 "freestream_demo_runs"),
                             samples=100, dwell_s=0.05,
                             move_timeout_s=5, tunnel_timeout_s=5)
    win = FreestreamMainWindow(config)              # falls back to fakes
    win.show()
    win.connect_btn.click()

    win.planner.alpha_edit.setText("-2:2:2")
    win.planner.mach_edit.setText("0.3")
    win.planner.build_btn.click()      # dwell/samples come from the config
    win.start_btn.click()

    deadline = time.monotonic() + 60
    while win.sweep_active and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)
    t_end = time.monotonic() + 1.0                 # let plots fill a bit
    while time.monotonic() < t_end:
        app.processEvents()
        time.sleep(0.01)

    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    ok = win.grab().save(out_path)
    print(f"screenshot {'saved' if ok else 'FAILED'}: {out_path}")
    win.close()
    app.processEvents()


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "freestream_gui.png")
