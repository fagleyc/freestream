"""Entry point: ``python -m freestream.app [--sim|--live] [--mode ...]``.

Sim is the default for now; ``--live`` opts into hardware. The GUI is
identical in both — only the adapters change.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from PyQt6.QtWidgets import QApplication

from .. import theme
from ..config import FreestreamConfig, defaults_path
from .main_window import FreestreamMainWindow


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="freestream", description="Freestream — wind tunnel suite GUI")
    parser.add_argument("--sim", action="store_true", default=True,
                        help="simulated adapters (default for now)")
    parser.add_argument("--live", action="store_true",
                        help="use real hardware (overrides --sim)")
    parser.add_argument("--mode", choices=("mode1", "mode2"), default=None,
                        help="device mode (default: config file / mode1)")
    parser.add_argument("--config", type=Path, default=None,
                        help="FreestreamConfig JSON to load")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(message)s")

    if args.config:
        config = FreestreamConfig.load(args.config)
    elif defaults_path().exists():
        # "Set as defaults" snapshot from a previous session — sample
        # rate, directories, output format + every device's driver config
        try:
            config = FreestreamConfig.load(defaults_path())
            logging.getLogger("freestream").info(
                "startup defaults loaded from %s", defaults_path())
        except Exception:                              # noqa: BLE001
            logging.getLogger("freestream").exception(
                "startup defaults unreadable — using factory settings")
            config = FreestreamConfig()
    else:
        config = FreestreamConfig()
    if args.mode:
        config.mode = args.mode
    config.sim = not args.live

    app = QApplication(sys.argv[:1])
    app.setApplicationName("Freestream")
    app.setStyleSheet(theme.get_stylesheet())
    theme.apply_pyqtgraph_theme()

    window = FreestreamMainWindow(config)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
