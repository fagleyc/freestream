"""Entry point:  ``python -m traverse_swt.app  [options]``.

    python -m traverse_swt.app --sim      # plant sim, no hardware
    python -m traverse_swt.app            # real PLC (192.168.1.21)
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from PyQt6.QtWidgets import QApplication

from traverse_swt import theme
from traverse_swt.config import TraverseConfig, load_startup_config


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="SSWT traverse (WAGO PLC)")
    ap.add_argument("--sim", action="store_true",
                    help="simulation mode — plant sim, no hardware")
    ap.add_argument("--ip", help="WAGO PLC IP (default 192.168.1.21)")
    ap.add_argument("--config", help="load a saved JSON config")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    # an explicit --config wins; otherwise the "Set as Defaults" file
    # (defaults_path()) auto-loads, guarded — factory defaults on error
    cfg = TraverseConfig.load(args.config) if args.config \
        else load_startup_config()
    if args.ip:
        cfg.ip = args.ip
    cfg.force_sim = args.sim

    from .main_window import TraverseMainWindow

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.get_stylesheet())
    win = TraverseMainWindow(cfg)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
