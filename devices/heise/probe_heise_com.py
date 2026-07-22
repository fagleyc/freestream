"""Search all COM ports for a Heise PM indicator (read-only '?').

Usage:  python probe_heise_com.py [--all-bauds] [--timeout 0.8]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from heise.comscan import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
