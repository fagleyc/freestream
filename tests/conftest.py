"""Make freestream + the device driver packages importable for pytest."""

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "devices"))
sys.path.insert(0, str(_ROOT.parent / "Streamlined"))
sys.path.insert(0, str(_ROOT.parent / "balance_cal"))


@pytest.fixture(autouse=True)
def _isolated_user_modes(tmp_path, monkeypatch):
    """Every test starts with an EMPTY user-saved-modes store.

    FreestreamMainWindow reads user_modes.json while building the mode
    combo, so a developer's real ~/.freestream/user_modes.json would
    otherwise leak ★ items into combo-content assertions (and tests
    could write into the real file). Tests that want their own store
    simply monkeypatch FREESTREAM_USER_MODES again — later setenv wins.
    """
    monkeypatch.setenv("FREESTREAM_USER_MODES",
                       str(tmp_path / "user_modes.json"))
