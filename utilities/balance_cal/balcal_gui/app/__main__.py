"""Standalone launcher: ``python -m balcal_gui.app [--sim] [--backend]``."""

from __future__ import annotations

import argparse
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Force/moment balance calibration (.vol) GUI")
    parser.add_argument("--sim", action="store_true",
                        help="simulated DAQ (no hardware)")
    parser.add_argument("--backend", default="ni6351",
                        choices=("ni6351", "strainbook"),
                        help="analog-input driver (default ni6351)")
    args = parser.parse_args(argv)

    from PyQt6.QtWidgets import QApplication

    from balcal_gui import theme
    from balcal_gui.app.main_window import BalanceCalWindow

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.get_stylesheet())
    win = BalanceCalWindow(backend=args.backend, sim=args.sim)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
