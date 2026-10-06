"""Rated element limits match across .vol naming variants.

Several force-balance files call the sixth element 'Mx' in the channel
sections but 'Roll' in [Maximal Balance Loads]; the old two-letter prefix
match missed it and the Freestream Forces page showed that bar as 'n/a'.
The 50 lb moment balance's .vol is tab-delimited, which the reader
rejected. Both variants are derived here from the real 100 lb file.
"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strainbook_616 import balcal  # noqa: E402

_CANDIDATES = [Path(__file__).resolve().parents[n] / "Streamlined" /
               "CalFiles" / "2025_06_06_2 100 lb.vol" for n in (2, 3)]
VOL = next((p for p in _CANDIDATES if p.exists()), None)
pytestmark = pytest.mark.skipif(VOL is None, reason="100 lb .vol not present")


def _variant(tmp_path, transform, name="variant.vol"):
    text = VOL.read_text(encoding="latin-1")
    out = tmp_path / name
    out.write_text(transform(text), encoding="latin-1")
    return balcal.read_vol_file(str(out))


def test_original_names_match():
    cal = balcal.read_vol_file(str(VOL))
    assert balcal.element_limits(cal) == [100.0, 100.0, 50.0, 50.0, 25.0,
                                          50.0]


def test_mx_channel_matches_a_roll_max_load(tmp_path):
    cal = _variant(tmp_path, lambda t: t.replace("Mx--> 50", "Roll--> 50"))
    assert cal.force_channels[5] == "Mx"
    assert "Roll" in cal.max_loads.values
    assert balcal.element_limits(cal)[5] == 50.0
    balcal.calc_coeffs(cal, "Linear")
    util = balcal.element_utilization(cal, np.ones((4, 6)))
    assert set(util) == set(cal.force_channels[:6])     # no 'n/a'


def test_punctuation_and_position_fallback(tmp_path):
    def rename(t):
        return (t.replace("N1--> 100", "N_1--> 100")
                 .replace("Ax--> 25", "AxialForce--> 25"))
    cal = _variant(tmp_path, rename)
    assert balcal.element_limits(cal)[0] == 100.0        # N_1 = N1
    assert balcal.element_limits(cal)[4] == 25.0         # synonym family


def test_tab_delimited_rows_parse(tmp_path):
    cal = _variant(tmp_path, lambda t: t.replace(", ", "\t"))
    assert len(cal.force_channels) == 6
    assert cal.force.shape[1] == 6
