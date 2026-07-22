#!/usr/bin/env python
"""Launcher for the Heise PM indicator GUI (pressure / temperature).

    python run_heise_app.py --sim           # no hardware
    python run_heise_app.py --port COM5     # real indicator
    python run_heise_app.py                 # use Search in the GUI
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from heise.app.__main__ import main   # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
