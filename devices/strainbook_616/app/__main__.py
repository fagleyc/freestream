"""Entry point:  ``python -m strainbook_616.app  [options]``.

Examples
--------
Pure simulation (no hardware, no DLL)::

    python -m strainbook_616.app --sim

Against the real StrainBook/616 (alias ``StrainBook_0``)::

    python -m strainbook_616.app
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from PyQt6.QtWidgets import QApplication

from strainbook_616 import theme
from strainbook_616.config import StrainbookConfig


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="StrainBook/616 bridge DAQ")
    ap.add_argument("--sim", action="store_true",
                    help="simulation mode — synthetic data, no DLL")
    ap.add_argument("--device", help="device alias (default StrainBook_0)")
    ap.add_argument("--rate", type=float, help="scan rate in Hz")
    ap.add_argument("--dll", help="explicit path to DaqX64.dll")
    ap.add_argument("--config", help="load a saved JSON config")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = StrainbookConfig.load(args.config) if args.config \
        else StrainbookConfig()
    if args.device:
        cfg.device_name = args.device
    if args.rate:
        cfg.scan_hz = args.rate
    if args.dll:
        cfg.dll_path = args.dll
    cfg.force_sim = args.sim

    from .main_window import StrainbookMainWindow

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.get_stylesheet())
    win = StrainbookMainWindow(cfg)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
