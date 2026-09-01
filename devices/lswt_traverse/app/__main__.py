"""Entry point: ``python -m lswt_traverse.app [--sim] [--port COMx]``."""

from __future__ import annotations

import argparse
import logging
import sys


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="South LSWT traverse (IDC SmartStep23 chain)")
    parser.add_argument("--sim", action="store_true",
                        help="run against the built-in chain emulator")
    parser.add_argument("--port", default=None,
                        help="COM port of the daisy chain")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    from PyQt6.QtWidgets import QApplication

    from lswt_traverse.config import TraverseConfig
    from .main_window import TraverseMainWindow

    config = TraverseConfig.load_defaults()
    if args.sim:
        config.force_sim = True
    if args.port:
        config.port = args.port

    app = QApplication(sys.argv[:1])
    app.setApplicationName("LSWT Traverse")
    win = TraverseMainWindow(config)
    win.resize(1000, 720)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
