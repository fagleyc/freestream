#!/usr/bin/env python
"""Launcher for the LSWT sting-drive GUI.

    python run_lswt_sting_app.py --sim      # no hardware
    python run_lswt_sting_app.py            # real drives on the configured COM
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lswt_sting.app.__main__ import main   # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
