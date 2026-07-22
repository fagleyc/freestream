"""Standalone launcher: ``python -m heise.app [--sim] [--port COM5]``."""

from __future__ import annotations

import argparse
import logging
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Heise PM indicator GUI (pressure / temperature)")
    parser.add_argument("--sim", action="store_true",
                        help="simulated indicator (no hardware)")
    parser.add_argument("--port", default="",
                        help="COM port (e.g. COM5); or use Search in "
                             "the GUI")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--config", default="",
                        help="load a saved heise_config.json")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from PyQt6.QtWidgets import QApplication

    from heise import theme
    from heise.config import HeiseConfig
    from heise.app.main_window import HeiseMainWindow

    cfg = HeiseConfig.load(args.config) if args.config else HeiseConfig()
    if args.port:
        cfg.com_port = args.port
    cfg.baud = args.baud
    cfg.force_sim = args.sim

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.get_stylesheet())
    win = HeiseMainWindow(cfg)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
