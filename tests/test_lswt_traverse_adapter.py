"""LswtTraverseAdapter (South LSWT SmartStep traverse) — sim unit tests.

Builds the adapter directly (as the DeviceManager would: ``cls(sim=True)``)
and exercises the Positioner capability against the driver's SimChain
emulator, plus the referencing story this rig replaces homing with:

* sim connect auto-references every axis (``set_home_all``) so status is
  OK and sweeps run out of the box;
* an un-referenced axis (the LIVE wake-up state — a SmartStep reads
  0.000 wherever it stands) is a FAULT with a clear message, and
  ``move_to`` refuses it;
* no counts calibration and no native settings dialog exist.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_PROJECTS = Path(__file__).resolve().parents[2]
for _p in (_PROJECTS / "freestream", _PROJECTS / "freestream" / "devices"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from freestream.adapters.lswt_traverse import LswtTraverseAdapter  # noqa: E402
from freestream.hal import FAULT, Positioner                       # noqa: E402


def _wait_settled(adapter, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if adapter.settled():
            return True
        time.sleep(0.05)
    return False


# ── identity / registration surface ──────────────────────────────────────
def test_identity_and_no_native_settings_dialog():
    a = LswtTraverseAdapter(sim=True)
    assert a.id == "lswt_traverse"
    assert "South LSWT traverse" in a.label
    # the device app ships no SettingsDialog — Freestream's generic
    # DeviceConfigDialog (Settings + axis tabs) is the editor
    assert a.settings_dialog_path == ""
    assert not a.has_settings()
    assert isinstance(a, Positioner)
    assert a.driver is not None                 # embedded panels hook


# ── sim positioner round-trip ────────────────────────────────────────────
def test_sim_positioner_round_trip():
    a = LswtTraverseAdapter(sim=True)
    a.connect()
    try:
        assert a.connected and a.sim
        # sim auto-references at connect → clear to record immediately
        assert a.status().ok
        axes = {ax.name: ax for ax in a.axes()}
        assert set(axes) == {"x", "y", "z"}
        for ax in axes.values():
            assert ax.unit == "in"
            assert ax.min < ax.max
            assert ax.tolerance > 0
        handle = a.move_to(x=2.0, y=-3.0, z=1.5)
        assert handle.targets == {"x": 2.0, "y": -3.0, "z": 1.5}
        assert _wait_settled(a), "traverse move did not settle in time"
        pos = a.positions()
        assert pos["x"] == pytest.approx(2.0, abs=0.05)
        assert pos["y"] == pytest.approx(-3.0, abs=0.05)
        assert pos["z"] == pytest.approx(1.5, abs=0.05)
        # partial move: untouched axes stay put
        a.move_to(x=0.0)
        assert _wait_settled(a)
        pos = a.positions()
        assert pos["x"] == pytest.approx(0.0, abs=0.05)
        assert pos["y"] == pytest.approx(-3.0, abs=0.05)
        a.stop_all()
    finally:
        a.disconnect()
    assert not a.connected


def test_unknown_axis_and_soft_limit_refused():
    a = LswtTraverseAdapter(sim=True)
    a.connect()
    try:
        with pytest.raises(ValueError, match="unknown axes"):
            a.move_to(alpha=1.0)
        # host-side soft travel limits gate every absolute target
        with pytest.raises(ValueError, match="soft limits"):
            a.move_to(x=999.0)
    finally:
        a.disconnect()


# ── referencing: the analogue of the SWT counts calibration ──────────────
def test_unreferenced_axes_fault_until_sim_connect_references():
    a = LswtTraverseAdapter(sim=True)
    # connect the DRIVER directly — the LIVE-like wake-up state: drives
    # answer but nothing is referenced yet (no operator Set home)
    a._drive.connect()
    try:
        st = a.status()
        assert st.state == FAULT
        assert "not referenced" in st.message
        assert "X" in st.message and "Y" in st.message and "Z" in st.message
        # absolute moves are refused until the axes are referenced
        with pytest.raises(ValueError, match="not referenced"):
            a.move_to(x=1.0)
        # the ADAPTER's sim connect references everything (set_home_all)
        a.connect()
        assert a.status().ok
        for name in ("X", "Y", "Z"):
            assert a._drive.is_referenced(name)
    finally:
        a.disconnect()


def test_partial_reference_names_only_the_missing_axis():
    a = LswtTraverseAdapter(sim=True)
    a.connect()
    try:
        assert a.status().ok
        # simulate a re-powered drive on ONE axis (reference lost)
        a._drive._state["Y"].referenced = False
        st = a.status()
        assert st.state == FAULT
        assert "Y" in st.message
        assert "X/" not in st.message and "/Z" not in st.message
    finally:
        a.disconnect()


# ── config provenance ────────────────────────────────────────────────────
def test_explicit_config_path_wins_and_sim_flag_is_session_owned(tmp_path):
    from lswt_traverse.config import TraverseConfig
    cfg = TraverseConfig()
    cfg.x.min_in, cfg.x.max_in = -5.0, 5.0
    cfg.force_sim = False                       # saved LIVE snapshot
    path = tmp_path / "traverse.json"
    cfg.save(path)
    a = LswtTraverseAdapter(sim=True, config_path=str(path))
    assert a.config.x.min_in == -5.0
    assert a.config.x.max_in == 5.0
    # the session's SIM/LIVE selection owns force_sim, not the file
    assert a.config.force_sim is True
    x_spec = next(ax for ax in a.axes() if ax.name == "x")
    assert (x_spec.min, x_spec.max) == (-5.0, 5.0)


def test_sim_speeds_up_the_emulated_axes():
    """The SimChain integrates at the commanded VE (velocity_ips); the
    rig-realistic 1 in/s would make sweep moves crawl — sim bumps it."""
    a = LswtTraverseAdapter(sim=True)
    for ax in a.config.axes():
        assert ax.velocity_ips >= 25.0
    live_cfg_defaults = [1.0, 1.0, 1.0]         # factory velocity_ips
    from lswt_traverse.config import TraverseConfig
    assert [ax.velocity_ips for ax in TraverseConfig().axes()] == \
        live_cfg_defaults
