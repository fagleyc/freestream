"""Entry point:  ``python -m lswt.app  [options]``.

    python -m lswt.app --tunnel north --sim   # fan sim, no hardware
    python -m lswt.app --tunnel south         # real ACS530 drive
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from PyQt6.QtWidgets import QApplication

from lswt import theme
from lswt.config import LswtConfig, load_startup_config


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(
        description="LSWT fan control (ABB ACS530)")
    ap.add_argument("--tunnel", choices=("north", "south"),
                    default="north", help="which tunnel (default north)")
    ap.add_argument("--sim", action="store_true",
                    help="simulation mode — fan model, no hardware")
    ap.add_argument("--ip", help="drive IP (overrides defaults)")
    ap.add_argument("--config", help="load a saved JSON config")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    # an explicit --config wins; otherwise the tunnel's "Set as
    # Defaults" file auto-loads (guarded — factory defaults on error)
    cfg = LswtConfig.load(args.config) if args.config \
        else load_startup_config(args.tunnel)
    if args.ip:
        cfg.ip = args.ip
    cfg.force_sim = args.sim

    from .main_window import LswtMainWindow

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.get_stylesheet())
    win = LswtMainWindow(cfg)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
