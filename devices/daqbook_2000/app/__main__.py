"""Entry point:  ``python -m daqbook_2000.app  [options]``.

Examples
--------
Pure simulation (no hardware, no DLL)::

    python -m daqbook_2000.app --sim

Against the real DaqBook/2005 (needs the DaqX software configured with the
device alias, see README)::

    python -m daqbook_2000.app --device DaqBook2005
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from PyQt6.QtWidgets import QApplication

from daqbook_2000 import theme
from daqbook_2000.config import DaqbookConfig


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="DaqBook/2000 tunnel DAQ client")
    ap.add_argument("--sim", action="store_true",
                    help="simulation mode — synthetic data, no DLL")
    ap.add_argument("--device", help="device alias (default DaqBook2005)")
    ap.add_argument("--rate", type=float, help="scan rate in Hz")
    ap.add_argument("--dll", help="explicit path to DaqX64.dll")
    ap.add_argument("--config", help="load a saved JSON config")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = DaqbookConfig.load(args.config) if args.config else DaqbookConfig()
    if args.device:
        cfg.device_name = args.device
    if args.rate:
        cfg.scan_hz = args.rate
    if args.dll:
        cfg.dll_path = args.dll
    cfg.force_sim = args.sim

    from .main_window import DaqbookMainWindow

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.get_stylesheet())
    win = DaqbookMainWindow(cfg)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
