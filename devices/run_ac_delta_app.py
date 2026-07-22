#!/usr/bin/env python
"""Launcher for the ARC Crescent sting-drive GUI.

    python run_ac_delta_app.py --sim   # no hardware
    python run_ac_delta_app.py         # real drives (Alpha .11 / Beta .12)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ac_delta.app.__main__ import main   # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
