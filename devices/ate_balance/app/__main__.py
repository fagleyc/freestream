"""Entry point:  ``python -m ate_balance.app  [options]``.

Examples
--------
Pure simulation (no hardware, no emulator)::

    python -m ate_balance.app --sim

Against the bundled Python emulator (in another terminal run
``python -m ate_balance.emulator --tms-ip 127.0.0.1``)::

    python -m ate_balance.app --ip 127.0.0.1

Against the real OGI on the rig network (static IP 192.168.1.60)::

    python -m ate_balance.app --ip 192.168.1.60
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Optional

from PyQt6.QtWidgets import QApplication

from ate_balance import theme
from ate_balance.config import CONNECT_DIAL, CONNECT_LISTEN, AteConfig


def build_config(args) -> AteConfig:
    if args.ogi_ini:
        cfg = AteConfig.from_ogi_ini(args.ogi_ini)
    else:
        cfg = AteConfig()
    if args.ip:
        cfg.ogi_ip = args.ip
    if args.tmsc:
        cfg.tmsc_port = args.tmsc
    if args.tmsd:
        cfg.tmsd_port = args.tmsd
    if args.ogit:
        cfg.ogit_port = args.ogit
    cfg.connect_mode = CONNECT_DIAL if args.dial else CONNECT_LISTEN
    cfg.force_sim = args.sim
    return cfg


def main(argv: Optional[list] = None) -> int:
    ap = argparse.ArgumentParser(description="ATE external balance TMS client")
    ap.add_argument("--sim", action="store_true",
                    help="simulation mode — synthetic data, no sockets")
    ap.add_argument("--ip", help="OGI / OGI_Sim IP (trigger + dial target)")
    ap.add_argument("--tmsc", type=int, help="TCP control port (default 3040)")
    ap.add_argument("--tmsd", type=int, help="UDP data port (default 3041)")
    ap.add_argument("--ogit", type=int, help="UDP trigger port (default 3042)")
    ap.add_argument("--dial", action="store_true",
                    help="actively dial the OGI instead of listening (rare)")
    ap.add_argument("--ogi-ini", help="seed ports/IP from a rig OGI.ini")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from .main_window import AteBalanceMainWindow

    app = QApplication(sys.argv[:1])
    app.setStyleSheet(theme.get_stylesheet())   # so dialogs match too
    win = AteBalanceMainWindow(build_config(args))
    win.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
