"""Entry point:  ``python -m ni_usb_6351.app  [options]``.

Examples
--------
Pure simulation (no hardware, no NI-DAQmx)::

    python -m ni_usb_6351.app --sim

Against the real USB-6351 (NI-MAX alias ``Dev2``)::

    python -m ni_usb_6351.app
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from PyQt6.QtWidgets import QApplication

from ni_usb_6351 import theme
from ni_usb_6351.config import NiDaqConfig


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="NI USB-6351 analog I/O DAQ")
    ap.add_argument("--sim", action="store_true",
                    help="simulation mode — synthetic data, no NI-DAQmx")
    ap.add_argument("--device", help="NI-MAX device alias (default Dev2)")
    ap.add_argument("--rate", type=float, help="scan rate in Hz")
    ap.add_argument("--config", help="load a saved JSON config")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    cfg = NiDaqConfig.load(args.config) if args.config \
        else NiDaqConfig()
    if args.device:
        cfg.device_name = args.device
    if args.rate:
        cfg.scan_hz = args.rate
    cfg.force_sim = args.sim

    from .main_window import NiDaqMainWindow

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.get_stylesheet())
    win = NiDaqMainWindow(cfg)
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
