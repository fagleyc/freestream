"""Search all COM ports for the LSWT sting indexer chain (read-only).

Usage:  python probe_sting_com.py [--baud 9600] [--timeout 0.7]
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lswt_sting.comscan import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
