"""Entry point:  ``python -m lswt_sting.app  [options]``.

    python -m lswt_sting.app --sim    # wire-level emulator, no hardware
    python -m lswt_sting.app          # real drives on the configured COM
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from PyQt6.QtWidgets import QApplication

from lswt_sting import theme
from lswt_sting.config import StingConfig


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="LSWT sting drive")
    ap.add_argument("--sim", action="store_true",
                    help="simulation mode — wire emulator, no hardware")
    ap.add_argument("--port", help="serial port (default COM1)")
    ap.add_argument("--config", help="load a saved JSON config")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = StingConfig.load(args.config) if args.config else StingConfig()
    if args.port:
        cfg.com_port = args.port
    if args.sim:
        cfg.force_sim = True

    from .main_window import StingMainWindow

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.get_stylesheet())
    win = StingMainWindow(cfg)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
