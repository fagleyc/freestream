"""AteConfig serialization tests — rated load maxima + legacy-key tolerance.

The model reference geometry (rho/S/c/b) moved to the Freestream suite; old
saved JSON configs that still carry those keys must load cleanly (unknown
keys are ignored).  ``max_loads`` holds the per-channel rated maxima (N for
Lift/Drag/Side, N·m for Pitch/Yaw/Roll; 0.0 = no limit) and must round-trip
through JSON.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ate_balance.config import AteConfig, RATED_LOADS_N
from ate_balance.protocol import WIRE_AXES


# ── max_loads defaults ───────────────────────────────────────────────────
def test_max_loads_default_to_the_published_ratings():
    """2026-08-21: the maxima used to default to 0.0 = "no limit",
    which drew no utilization bar at all and read as a dead readout.
    They now seed from the balance's published design load ranges
    (AID-010-10015-1 2.4), held in N / N*m."""
    cfg = AteConfig()
    assert set(cfg.max_loads) == set(WIRE_AXES) == \
        {"Lift", "Pitch", "Drag", "Side", "Yaw", "Roll"}
    assert cfg.max_loads == RATED_LOADS_N
    assert cfg.max_load_units == "N"
    # instances must not share the default dict
    cfg.max_loads["Lift"] = 100.0
    assert AteConfig().max_loads["Lift"] == RATED_LOADS_N["Lift"]


def test_max_loads_json_round_trip(tmp_path):
    cfg = AteConfig()
    cfg.max_loads.update({"Lift": 450.0, "Drag": 225.5, "Pitch": 56.5})
    path = tmp_path / "ate.json"
    cfg.save(path)
    loaded = AteConfig.load(path)
    assert loaded.max_loads == cfg.max_loads
    assert loaded.to_dict()["max_loads"]["Lift"] == 450.0
    assert loaded.max_loads["Yaw"] == RATED_LOADS_N["Yaw"]   # untouched


def test_max_loads_partial_dict_filled_in():
    # hand-edited / partial JSON: missing axes fall back to the rating
    cfg = AteConfig.from_dict({"max_loads": {"Lift": 300.0}})
    assert cfg.max_loads["Lift"] == 300.0
    assert set(cfg.max_loads) == set(WIRE_AXES)
    assert cfg.max_loads["Roll"] == RATED_LOADS_N["Roll"]


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
