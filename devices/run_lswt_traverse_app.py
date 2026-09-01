#!/usr/bin/env python
"""Launcher for the South LSWT traverse GUI (IDC SmartStep23 chain).

    python run_lswt_traverse_app.py --sim          # no hardware
    python run_lswt_traverse_app.py --port COM7    # real chain
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lswt_traverse.app.__main__ import main   # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
