#!/usr/bin/env python
"""Launcher for the SSWT traverse GUI (WAGO 750 PLC).

    python run_traverse_swt_app.py --sim   # no hardware
    python run_traverse_swt_app.py         # real PLC (192.168.1.21)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from traverse_swt.app.__main__ import main   # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
