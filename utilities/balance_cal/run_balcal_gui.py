"""Launch the balance calibration GUI.

Usage:  python run_balcal_gui.py [--sim] [--backend ni6351|strainbook]
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT.parents[1] / "devices"))

from balcal_gui.app.__main__ import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
