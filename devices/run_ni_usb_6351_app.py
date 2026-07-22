#!/usr/bin/env python
"""Launcher for the NI USB-6351 analog I/O DAQ GUI.

    python run_ni_usb_6351_app.py --sim     # no hardware
    python run_ni_usb_6351_app.py           # real USB-6351 (NI-MAX alias Dev2)
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ni_usb_6351.app.__main__ import main   # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
