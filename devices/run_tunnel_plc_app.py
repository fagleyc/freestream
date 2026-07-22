#!/usr/bin/env python
"""Launcher for the SSWT tunnel GUI (Red Lion G315 gateway).

    python run_tunnel_plc_app.py --sim   # no hardware
    python run_tunnel_plc_app.py         # real gateway (192.168.1.50)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from tunnel_plc.app.__main__ import main   # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
