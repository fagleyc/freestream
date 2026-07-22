#!/usr/bin/env python
"""Launcher for the StrainBook/616 balance-bridge DAQ GUI.

    python run_strainbook_616_app.py --sim     # no hardware
    python run_strainbook_616_app.py           # real StrainBook_0 (192.168.1.123)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from strainbook_616.app.__main__ import main   # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
