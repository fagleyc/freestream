"""Entry point:  ``python -m tunnel_plc.app  [options]``.

    python -m tunnel_plc.app --sim      # plant sim, no hardware
    python -m tunnel_plc.app            # real gateway (192.168.1.50)
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from PyQt6.QtWidgets import QApplication

from tunnel_plc import theme
from tunnel_plc.config import TunnelConfig


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="SSWT tunnel (Red Lion G315)")
    ap.add_argument("--sim", action="store_true",
                    help="simulation mode — plant sim, no hardware")
    ap.add_argument("--ip", help="Red Lion IP (default 192.168.1.50)")
    ap.add_argument("--config", help="load a saved JSON config")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = TunnelConfig.load(args.config) if args.config else TunnelConfig()
    if args.ip:
        cfg.ip = args.ip
    cfg.force_sim = args.sim

    from .main_window import TunnelMainWindow

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.get_stylesheet())
    win = TunnelMainWindow(cfg)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
