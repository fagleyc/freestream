"""Make freestream + the device driver packages importable for pytest."""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "devices"))
sys.path.insert(0, str(_ROOT.parent / "Streamlined"))
sys.path.insert(0, str(_ROOT.parent / "balance_cal"))
