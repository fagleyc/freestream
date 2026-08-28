"""AteConfig serialization tests — rated load maxima + legacy-key tolerance.

The model reference geometry (rho/S/c/b) moved to the Freestream suite; old
saved JSON configs that still carry those keys must load cleanly (unknown
keys are ignored).  ``max_loads`` holds the per-channel rated maxima keyed by
the BALANCE-FRAME axes (N for Fx/Fy/Fz, N·m for Mx/My/Mz; 0.0 = no limit)
and must round-trip through JSON; configs saved before the balance-frame
rename carried the wire names (Lift/Drag/...) and must migrate on load.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ate_balance.config import AteConfig, RATED_LOADS_N
from ate_balance.protocol import BALANCE_AXES


# ── max_loads defaults ───────────────────────────────────────────────────
def test_max_loads_default_to_the_published_ratings():
    """2026-08-21: the maxima used to default to 0.0 = "no limit",
    which drew no utilization bar at all and read as a dead readout.
    They now seed from the balance's published design load ranges
    (AID-010-10015-1 2.4), held in N / N*m, keyed by the balance axes."""
    cfg = AteConfig()
    assert set(cfg.max_loads) == set(BALANCE_AXES) == \
        {"Fx", "Fy", "Fz", "Mx", "My", "Mz"}
    assert cfg.max_loads == RATED_LOADS_N
    assert cfg.max_load_units == "N"
    # instances must not share the default dict
    cfg.max_loads["Fz"] = 100.0
    assert AteConfig().max_loads["Fz"] == RATED_LOADS_N["Fz"]


def test_max_loads_json_round_trip(tmp_path):
    cfg = AteConfig()
    cfg.max_loads.update({"Fz": 450.0, "Fx": 225.5, "My": 56.5})
    path = tmp_path / "ate.json"
    cfg.save(path)
    loaded = AteConfig.load(path)
    assert loaded.max_loads == cfg.max_loads
    assert loaded.to_dict()["max_loads"]["Fz"] == 450.0
    assert loaded.max_loads["Mz"] == RATED_LOADS_N["Mz"]   # untouched


def test_max_loads_partial_dict_filled_in():
    # hand-edited / partial JSON: missing axes fall back to the rating
    cfg = AteConfig.from_dict({"max_loads": {"Fz": 300.0}})
    assert cfg.max_loads["Fz"] == 300.0
    assert set(cfg.max_loads) == set(BALANCE_AXES)
    assert cfg.max_loads["Mx"] == RATED_LOADS_N["Mx"]


def test_max_loads_legacy_wire_keys_migrate():
    """Configs saved before the balance-frame rename keyed max_loads by
    the wire names.  They must load with values preserved under the new
    axes and no wire keys left behind:
    Lift→Fz, Drag→Fx, Side→Fy, Roll→Mx, Pitch→My, Yaw→Mz."""
    legacy = {"max_loads": {"Lift": 450.0, "Drag": 225.5, "Side": 999.0,
                            "Roll": 111.0, "Pitch": 56.5, "Yaw": 77.0}}
    cfg = AteConfig.from_dict(legacy)
    assert set(cfg.max_loads) == set(BALANCE_AXES)
    assert cfg.max_loads["Fz"] == 450.0
    assert cfg.max_loads["Fx"] == 225.5
    assert cfg.max_loads["Fy"] == 999.0
    assert cfg.max_loads["Mx"] == 111.0
    assert cfg.max_loads["My"] == 56.5
    assert cfg.max_loads["Mz"] == 77.0
    # a new-name entry wins over a stale legacy one for the same axis
    cfg2 = AteConfig.from_dict(
        {"max_loads": {"Fz": 500.0, "Lift": 450.0}})
    assert cfg2.max_loads["Fz"] == 500.0
    assert "Lift" not in cfg2.max_loads


# ── legacy geometry keys ─────────────────────────────────────────────────
def test_old_configs_with_geometry_keys_still_load():
    # pre-removal configs carried the reference geometry; those keys are
    # unknown now and must be silently ignored on load
    legacy = {"ogi_ip": "10.0.0.9", "rho_kg_m3": 1.05,
              "ref_area_m2": 0.0929, "ref_chord_m": 0.127,
              "ref_span_m": 0.762}
    cfg = AteConfig.from_dict(legacy)
    assert cfg.ogi_ip == "10.0.0.9"
    for gone in ("rho_kg_m3", "ref_area_m2", "ref_chord_m", "ref_span_m"):
        assert not hasattr(cfg, gone)
        assert gone not in cfg.to_dict()
