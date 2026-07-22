"""AteBalanceAdapter — Streaming + Positioner + Zeroable over ate_balance.

Mode 2 all-in-one: the ATE external balance measures forces/moments AND
drives alpha/beta, so ONE adapter fills both registry roles.

Driver realities accommodated here (see ate_balance.device/protocol):

* Loads arrive as callback frames (``on_frame(BalanceFrame)``) with the
  six WIRE-ORDER wind-axis loads ``Lift, Pitch, Drag, Side, Yaw, Roll``
  in N / N.m — there is no driver-side ring buffer, so this adapter
  accumulates frames itself for :meth:`drain_block`.
* For a Streamlined-identical HDF5 the loads are recorded in group
  ``StrainBook_0`` under the Mode 1 balance channel names via
  ``LOAD_MAP`` (default N1←Lift, N2←Pitch, Y1←Side, Y2←Yaw, Axial←Drag,
  Roll←Roll). NOTE: these are RESOLVED wind-axis loads (N / N.m), not
  bridge volts — the mapping is an aliasing for file-layout parity, and
  the units in :meth:`channels` say so honestly.
* Motion is command/reply over TMSC: ``goto_inc`` (alpha, −10…45°) and
  ``goto_yaw`` (beta, −90…90°) answer ``*_MOVING`` then ``*_COMPLETE``
  asynchronously via ``on_reply`` — ``settled()`` tracks those replies,
  and per-serial bookkeeping clears a move on an ``ERROR`` reply.
* ``zero()`` sends the OGI ZERO command and waits for the ``TARES``
  reply (the OGI averages internally; ``seconds`` only pads the wait).

Position channels stream in group ``Positioner`` (Alpha/Beta, deg),
sampled per load frame from the cached reply positions. The recorder
owns time — no Time channel.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

_DEVICES_DIR = Path(__file__).resolve().parents[3] / "devices"
if str(_DEVICES_DIR) not in sys.path:
    sys.path.insert(0, str(_DEVICES_DIR))

from ate_balance import protocol as P                         # noqa: E402
from ate_balance.config import AteConfig                      # noqa: E402
from ate_balance.device import AteBalanceDevice               # noqa: E402

from ..hal import (AxisSpec, ChannelSpec, DeviceStatus,       # noqa: E402
                   MoveHandle, OFFLINE, OK)

LOAD_GROUP = "StrainBook_0"
POS_GROUP = "Positioner"

# Streamlined balance-channel name → ATE wire-axis name. Forces pair
# with force-like channels, moments with moment-like ones; override via
# the ``load_map`` option if the campaign defines a different aliasing.
LOAD_MAP: Dict[str, str] = {
    "N1": "Lift", "N2": "Pitch", "Y1": "Side",
    "Y2": "Yaw", "Axial": "Drag", "Roll": "Roll",
}
_FORCE_AXES = ("Lift", "Drag", "Side")


class AteBalanceAdapter:
    """Streaming + Positioner + Zeroable adapter for the ATE balance."""

    id = "ate"
    label = "ATE external balance (loads + alpha/beta)"

    def __init__(self, sim: bool = False,
                 config_path: Optional[str] = None,
                 sample_rate_hz: float = 50.0,
                 tolerance_deg: float = 0.05,
                 load_map: Optional[Dict[str, str]] = None):
        cfg = (AteConfig.load(config_path) if config_path
               else AteConfig())
        cfg.force_sim = bool(sim)
        self._cfg = cfg
        self._dev = AteBalanceDevice(cfg)
        self._sim = bool(sim)
        self._rate = float(sample_rate_hz)
        self._tol = float(tolerance_deg)
        self._map = dict(load_map or LOAD_MAP)

        self._lock = threading.RLock()
        self._acc: Dict[str, list] = self._empty_acc()
        self._latest: Dict[str, float] = {}
        self._last_frame_t: Optional[float] = None

        self._alpha = 0.0                 # INC position (deg)
        self._beta = 0.0                  # YAW position (deg)
        self._alpha_moving = False
        self._beta_moving = False
        self._pending: Dict[int, str] = {}   # goto serial → axis name

        self._tares: Dict[str, float] = {}
        self._tare_evt = threading.Event()
        self._pos_evt = threading.Event()

        self._dev.on_frame = self._on_frame
        self._dev.on_reply = self._on_reply

    def _empty_acc(self) -> Dict[str, list]:
        acc: Dict[str, list] = {name: [] for name in self._map}
        acc["Alpha"] = []
        acc["Beta"] = []
        return acc

    # ── driver callbacks (IO/timer threads) ──────────────────────────────
    def _on_frame(self, bf) -> None:
        with self._lock:
            for name, wire in self._map.items():
                v = bf.loads.get(wire, 0.0)
                self._acc[name].append(v)
                self._latest[name] = v
            self._acc["Alpha"].append(self._alpha)
            self._acc["Beta"].append(self._beta)
            self._latest["Alpha"] = self._alpha
            self._latest["Beta"] = self._beta
            self._last_frame_t = time.time()

    def _on_reply(self, msg) -> None:
        cmd, vals = msg.command, msg.float_params()
        with self._lock:
            if cmd == P.RSP_POSITIONS and len(vals) >= 2:
                self._beta, self._alpha = vals[0], vals[1]   # yaw, inc
                self._pos_evt.set()
            elif cmd == P.RSP_INC_COMPLETE:
                if vals:
                    self._alpha = vals[0]
                self._alpha_moving = False
                self._pending.pop(msg.serial, None)
            elif cmd == P.RSP_YAW_COMPLETE:
                if vals:
                    self._beta = vals[0]
                self._beta_moving = False
                self._pending.pop(msg.serial, None)
            elif cmd == P.RSP_TARES and len(vals) >= 6:
                self._tares = {
                    name: vals[P.WIRE_AXES.index(wire)]
                    for name, wire in self._map.items()}
                self._tare_evt.set()
            elif cmd == P.RSP_ERROR:
                axis = self._pending.pop(msg.serial, None)
                if axis == "alpha":
                    self._alpha_moving = False
                elif axis == "beta":
                    self._beta_moving = False

    # ── DeviceBase ───────────────────────────────────────────────────────
    def connect(self) -> None:
        self._dev.connect()
        with self._lock:
            self._acc = self._empty_acc()
            self._last_frame_t = None
        self._dev.get_positions()         # seed the position cache

    def disconnect(self) -> None:
        self._dev.disconnect()

    @property
    def connected(self) -> bool:
        return self._dev.connected

    @property
    def sim(self) -> bool:
        return self._sim

    def status(self) -> DeviceStatus:
        if not self._dev.connected:
            return DeviceStatus(state=OFFLINE, message="not connected",
                                sim=self._sim)
        if not self._dev.link_up:
            return DeviceStatus(state=OFFLINE, sim=self._sim,
                                message="TMSC control link down")
        with self._lock:
            age = (None if self._last_frame_t is None
                   else max(time.time() - self._last_frame_t, 0.0))
        return DeviceStatus(state=OK, sim=self._sim,
                            last_sample_age_s=age)

    # ── Streaming ────────────────────────────────────────────────────────
    def start(self) -> None:
        self._dev.start()

    def stop(self) -> None:
        self._dev.stop()

    def sample_rate(self) -> float:
        return self._rate

    def channels(self) -> List[ChannelSpec]:
        specs = []
        for name, wire in self._map.items():
            unit = "N" if wire in _FORCE_AXES else "N*m"
            specs.append(ChannelSpec(name=name, unit=unit,
                                     group=LOAD_GROUP, kind="raw",
                                     device_id=self.id))
        for name in ("Alpha", "Beta"):
            specs.append(ChannelSpec(name=name, unit="deg",
                                     group=POS_GROUP, kind="position",
                                     device_id=self.id))
        return specs

    def latest(self) -> Dict[str, float]:
        with self._lock:
            return dict(self._latest)

    def drain_block(self) -> Dict[str, np.ndarray]:
        """All frames accumulated since the previous drain."""
        with self._lock:
            acc, self._acc = self._acc, self._empty_acc()
        return {name: np.asarray(vals, dtype=np.float64)
                for name, vals in acc.items()}

    # ── Positioner ───────────────────────────────────────────────────────
    def axes(self) -> List[AxisSpec]:
        return [
            AxisSpec(name="alpha", unit="deg",
                     min=P.INC_LIMITS_DEG[0], max=P.INC_LIMITS_DEG[1],
                     tolerance=self._tol),
            AxisSpec(name="beta", unit="deg",
                     min=P.YAW_LIMITS_DEG[0], max=P.YAW_LIMITS_DEG[1],
                     tolerance=self._tol),
        ]

    def move_to(self, **axes: float) -> MoveHandle:
        unknown = set(axes) - {"alpha", "beta"}
        if unknown:
            raise ValueError(f"unknown axes {sorted(unknown)}; "
                             f"ate has alpha/beta")
        if not self._dev.connected:
            raise RuntimeError("connect() first")
        for name, limits in (("alpha", P.INC_LIMITS_DEG),
                             ("beta", P.YAW_LIMITS_DEG)):
            if name in axes and not (limits[0] <= axes[name]
                                     <= limits[1]):
                raise ValueError(
                    f"{name} target {axes[name]:+.2f}° outside limits "
                    f"[{limits[0]:+.1f}, {limits[1]:+.1f}]")
        with self._lock:
            if "alpha" in axes:
                self._alpha_moving = True
                serial = self._dev.goto_inc(float(axes["alpha"]))
                self._pending[serial] = "alpha"
            if "beta" in axes:
                self._beta_moving = True
                serial = self._dev.goto_yaw(float(axes["beta"]))
                self._pending[serial] = "beta"
        return MoveHandle(targets=dict(axes))

    def positions(self) -> Dict[str, float]:
        self._pos_evt.clear()
        self._dev.get_positions()
        self._pos_evt.wait(timeout=1.0)   # sim replies synchronously
        with self._lock:
            return {"alpha": self._alpha, "beta": self._beta}

    def settled(self) -> bool:
        with self._lock:
            return not (self._alpha_moving or self._beta_moving)

    def stop_all(self) -> None:
        self._dev.stop_all_motion()
        with self._lock:
            self._alpha_moving = False
            self._beta_moving = False
            self._pending.clear()

    # ── Zeroable ─────────────────────────────────────────────────────────
    def zero(self, seconds: float = 0.5) -> Dict[str, float]:
        """OGI ZERO; waits for the TARES reply and returns it mapped."""
        self._tare_evt.clear()
        self._dev.zero()
        if not self._tare_evt.wait(timeout=seconds + 3.0):
            return {}
        with self._lock:
            return dict(self._tares)
