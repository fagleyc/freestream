#!/usr/bin/env python
"""Launcher for the DaqBook/2000 tunnel-conditions DAQ GUI.

    python run_daqbook_2000_app.py --sim        # no hardware
    python run_daqbook_2000_app.py              # real DaqBook2005 (needs DaqX setup)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from daqbook_2000.app.__main__ import main   # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
