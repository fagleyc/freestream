"""Entry point:  ``python -m ac_delta.app  [options]``.

    python -m ac_delta.app --sim      # physics sim, no hardware
    python -m ac_delta.app            # real drives (.11 Alpha / .12 Beta)
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from PyQt6.QtWidgets import QApplication

from ac_delta import theme
from ac_delta.config import CrescentConfig


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="ARC Crescent sting drive")
    ap.add_argument("--sim", action="store_true",
                    help="simulation mode — physics sim, no hardware")
    ap.add_argument("--alpha-ip", help="Alpha drive IP (default 192.168.1.11)")
    ap.add_argument("--beta-ip", help="Beta drive IP (default 192.168.1.12)")
    ap.add_argument("--config", help="load a saved JSON config")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = CrescentConfig.load(args.config) if args.config \
        else CrescentConfig()
    if args.alpha_ip:
        cfg.alpha.ip = args.alpha_ip
    if args.beta_ip:
        cfg.beta.ip = args.beta_ip
    cfg.force_sim = args.sim

    from .main_window import CrescentMainWindow

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.get_stylesheet())
    win = CrescentMainWindow(cfg)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
