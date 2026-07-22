#!/usr/bin/env python
"""Launcher for the ATE balance TMS client GUI.

Run from the ``devices`` directory (or anywhere — the package path is added
automatically):

    python run_ate_balance_app.py --sim                 # no hardware
    python run_ate_balance_app.py --ip 127.0.0.1        # against the Python emulator
    python run_ate_balance_app.py                       # against the real rig (192.168.1.60)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ate_balance.app.__main__ import main   # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
