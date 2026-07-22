#!/usr/bin/env python
"""Launcher for the LSWT fan-control GUI (ABB ACS530 drives).

    python run_lswt_app.py --tunnel north --sim   # no hardware
    python run_lswt_app.py --tunnel south         # real drive
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lswt.app.__main__ import main   # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
