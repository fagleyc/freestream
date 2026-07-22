#!/usr/bin/env python
"""Launcher for the Freestream wind-tunnel suite.

    python run_freestream.py --sim                       # whole suite, no HW
    python run_freestream.py --mode SWT-External         # ATE balance mode
    python run_freestream.py --mode LSWT-LSWTSting-NI    # North LSWT rig
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "devices"))

from freestream.app.__main__ import main   # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
