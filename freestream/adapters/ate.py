"""AteBalanceAdapter — Streaming + Positioner + Zeroable over ate_balance.

Mode 2 all-in-one: the ATE external balance measures forces/moments AND
drives alpha/beta, so ONE adapter fills both registry roles.

Driver realities accommodated here (see ate_balance.device/protocol):

* Loads arrive as callback frames (``on_frame(BalanceFrame)``) keyed by
  the six BALANCE-FRAME axes ``Fx, Fy, Fz, Mx, My, Mz`` (X back, Y
  right, Z up) — there is no driver-side ring buffer, so this adapter
  accumulates frames itself for :meth:`drain_block`.
* The loads are recorded TRUTHFULLY: group ``ATE_Balance`` under the
  balance-frame names ``Fx, Fy, Fz, Mx, My, Mz``. The wire protocol's
  Lift/Drag/Side words are gone from the records — the device measures
  balance-frame components, and calling them wind-axis loads baked in
  the full-span assumption (in ½ span the vertical channel is the
  model's SIDE force). Wind resolution is span-aware and happens in the
  reduction. Downstream readers key off the ``balance_group``/
  ``balance_type`` file markers; legacy archives recorded under the old
  wind names (or the still older N1… aliases) are mapped on read. A
  campaign-specific renaming can still be injected via the ``load_map``
  constructor option (``{recorded name: balance axis}``).
* Motion is command/reply over TMSC and answers ``*_MOVING`` then
  ``*_COMPLETE`` asynchronously via ``on_reply`` — ``settled()`` tracks
  those replies, and per-serial bookkeeping clears a move on an
  ``ERROR`` reply. The DRIVER owns the model-span mapping
  (``AteConfig.span_config``): this adapter commands logical alpha/beta
  through ``goto_alpha``/``goto_beta`` and the driver resolves the
  physical drive — full span: alpha=incidence (−10…45°), beta=yaw
  (−90…90°); ½ span: alpha=THE YAW DRIVE (−90…90°), no beta axis, the
  incidence drive never commanded. ``axes()``/``positions()``/
  ``channels()`` follow the same mapping, so a ½-span rig exposes an
  alpha-only Positioner.
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
from collections import deque
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

_DEVICES_DIR = Path(__file__).resolve().parents[2] / "devices"
if str(_DEVICES_DIR) not in sys.path:
    sys.path.insert(0, str(_DEVICES_DIR))

from ate_balance import protocol as P                         # noqa: E402
from ate_balance.config import (AteConfig, LOAD_UNITS,        # noqa: E402
                                convert_loads)
from ate_balance.device import AteBalanceDevice               # noqa: E402

from ..hal import (AxisSpec, ChannelSpec, DeviceStatus,       # noqa: E402
                   MoveHandle, OFFLINE, OK)
from ._configurable import ConfigurableAdapter                 # noqa: E402

LOAD_GROUP = "ATE_Balance"
POS_GROUP = "Positioner"

# Default recorded-name → balance-axis mapping: IDENTITY over the six
# balance-frame axes (truth-naming). Override via the ``load_map`` option
# only if a campaign genuinely needs different recorded names.
LOAD_MAP: Dict[str, str] = {name: name for name in P.BALANCE_AXES}
_FORCE_AXES = ("Fx", "Fy", "Fz")


class AteBalanceAdapter(ConfigurableAdapter):
    """Streaming + Positioner + Zeroable adapter for the ATE balance."""

    id = "ate"
    label = "ATE external balance (loads + alpha/beta)"
    settings_dialog_path = "ate_balance.app.settings_dialog:SettingsDialog"
    #: hardware classification, inherited into the recorded file markers
    #: (root attr ``balance_type``) by the sweep engine — generic, never
    #: hardcoded per mode.
    balance_type = "external"

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
        self._disp: Dict[str, deque] = {
            name: deque(maxlen=self.DISPLAY_TAIL_FRAMES) for name in self._map}
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
        if self._has_beta:
            acc["Beta"] = []
        return acc

    @property
    def _has_beta(self) -> bool:
        """True when the driver's span mapping exposes a beta axis."""
        return self._dev.beta_limits() is not None

    # ── display-only tail ────────────────────────────────────────────────
    #: frames kept purely so the monitor can low-pass its view. Separate
    #: from ``_acc``, which the recorder DRAINS — peeking at that would
    #: race the recording, and this must never affect what is recorded.
    DISPLAY_TAIL_FRAMES = 2048

    def display_tail(self, n: int) -> Dict[str, np.ndarray]:
        """Last ``n`` frames per load channel, WITHOUT consuming them.

        For live readouts only. Returns whatever is available (possibly
        fewer than ``n``, possibly empty)."""
        with self._lock:
            return {name: np.asarray(list(buf)[-n:], dtype=np.float64)
                    for name, buf in self._disp.items()}

    # ── driver callbacks (IO/timer threads) ──────────────────────────────
    def _on_frame(self, bf) -> None:
        with self._lock:
            for name, axis in self._map.items():
                v = bf.loads.get(axis, 0.0)
                self._acc[name].append(v)
                self._disp[name].append(v)
                self._latest[name] = v
            self._acc["Alpha"].append(self._alpha)
            self._latest["Alpha"] = self._alpha
            if "Beta" in self._acc:
                self._acc["Beta"].append(self._beta)
                self._latest["Beta"] = self._beta
            self._last_frame_t = time.time()

    def _on_reply(self, msg) -> None:
        cmd, vals = msg.command, msg.float_params()
        with self._lock:
            if cmd == P.RSP_POSITIONS and len(vals) >= 2:
                # wire order is (yaw, inc); the DRIVER maps physical
                # drives → logical axes per span_config
                mapped = self._dev.map_positions(vals[0], vals[1])
                self._alpha = mapped.get("alpha", self._alpha)
                if "beta" in mapped:
                    self._beta = mapped["beta"]
                self._pos_evt.set()
            elif cmd in (P.RSP_INC_COMPLETE, P.RSP_YAW_COMPLETE):
                axis = self._dev.logical_axis_for_reply(cmd)
                if axis == "alpha":
                    if vals:
                        self._alpha = vals[0]
                    self._alpha_moving = False
                elif axis == "beta":
                    if vals:
                        self._beta = vals[0]
                    self._beta_moving = False
                self._pending.pop(msg.serial, None)
            elif cmd == P.RSP_TARES and len(vals) >= 6:
                # TARES replies are wire-ordered; BALANCE_WIRE_INDEX maps a
                # balance axis to its slot in that order
                self._tares = {
                    name: vals[P.BALANCE_WIRE_INDEX[axis]]
                    for name, axis in self._map.items()}
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
        # FIXED: the OGI pushes frames at its own rate — this adapter
        # deliberately has NO set_sample_rate(); it reports the true frame
        # rate rather than pretending to follow the suite-wide setting.
        return self._rate

    def channels(self) -> List[ChannelSpec]:
        specs = []
        # The OGI's engineering-unit setting is not carried on the wire,
        # so the configured system is what gets stamped onto the recorded
        # channels — and that stamp is the ONLY thing telling the
        # reduction what to convert from. Keep it matched to the OGI.
        force_u, moment_u = LOAD_UNITS[self._cfg.load_units]
        for name, axis in self._map.items():
            unit = force_u if axis in _FORCE_AXES else moment_u
            specs.append(ChannelSpec(name=name, unit=unit,
                                     group=LOAD_GROUP, kind="raw",
                                     device_id=self.id))
        pos_names = ("Alpha", "Beta") if self._has_beta else ("Alpha",)
        for name in pos_names:
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
    @property
    def span_config(self) -> str:
        """Model-span configuration ("full" | "half") — the sweep engine
        inherits this into the recorded file's root attrs so
        post-processing can interpret the Positioner channels."""
        return self._dev.span_config

    def extra_meta(self) -> Dict[str, str]:
        """Extra per-device metadata merged into /meta/devices/ate."""
        return {"span_config": self._dev.span_config,
                "balance_type": self.balance_type,
                "load_units": self._cfg.load_units}

    @property
    def load_limits(self) -> Dict[str, float]:
        """Rated max per recorded load channel, IN THE STREAMED UNITS.

        The config holds the maxima in ``max_load_units`` (N / N*m by
        default) so they survive a change to the OGI's unit setting;
        this converts them into whatever ``load_units`` the balance is
        actually sending, because that is what the utilization bars
        divide. Without the conversion a pound-streaming balance judged
        against newton maxima reads 4.45x low and the bars all but
        vanish.

        Defensive: fields may be missing on older driver configs
        (``getattr`` defaults), and a zero/invalid entry means "no
        limit" (0.0) — the Forces page shows the raw value instead of a
        fake utilization for those channels."""
        raw = getattr(self._cfg, "max_loads", {}) or {}
        basis = getattr(self._cfg, "max_load_units", "N")
        streamed = getattr(self._cfg, "load_units", "N")
        limits: Dict[str, float] = {}
        for name, axis in self._map.items():
            try:
                v = float(raw.get(axis, 0.0) or 0.0)
            except (TypeError, ValueError):
                v = 0.0
            if v > 0 and basis != streamed:
                try:
                    v = convert_loads(v, basis, streamed,
                                      moment=axis not in _FORCE_AXES)
                except KeyError:                       # unknown unit name
                    pass
            limits[name] = v if v > 0 else 0.0
        return limits

    def axes(self) -> List[AxisSpec]:
        """Logical axes per the DRIVER's span mapping: full span exposes
        alpha (incidence limits) + beta (yaw limits); ½ span exposes
        alpha ONLY, with the YAW drive's limits."""
        a_lo, a_hi = self._dev.alpha_limits()
        specs = [AxisSpec(name="alpha", unit="deg", min=a_lo, max=a_hi,
                          tolerance=self._tol)]
        beta = self._dev.beta_limits()
        if beta is not None:
            specs.append(AxisSpec(name="beta", unit="deg",
                                  min=beta[0], max=beta[1],
                                  tolerance=self._tol))
        return specs

    def move_to(self, **axes: float) -> MoveHandle:
        valid = {a.name for a in self.axes()}
        unknown = set(axes) - valid
        if unknown:
            note = ("" if self._has_beta else
                    " (½-span configuration: no beta axis — alpha is the "
                    "yaw drive)")
            raise ValueError(f"unknown axes {sorted(unknown)}; "
                             f"ate has {'/'.join(sorted(valid))}{note}")
        if not self._dev.connected:
            raise RuntimeError("connect() first")
        limits = {a.name: (a.min, a.max) for a in self.axes()}
        for name in axes:
            lo, hi = limits[name]
            if not (lo <= axes[name] <= hi):
                raise ValueError(
                    f"{name} target {axes[name]:+.2f}° outside limits "
                    f"[{lo:+.1f}, {hi:+.1f}]")
        with self._lock:
            # the DRIVER resolves alpha/beta → physical drive (goto_alpha/
            # goto_beta) so the span mapping lives in exactly one place
            if "alpha" in axes:
                self._alpha_moving = True
                serial = self._dev.goto_alpha(float(axes["alpha"]))
                self._pending[serial] = "alpha"
            if "beta" in axes:
                self._beta_moving = True
                serial = self._dev.goto_beta(float(axes["beta"]))
                self._pending[serial] = "beta"
        return MoveHandle(targets=dict(axes))

    def positions(self) -> Dict[str, float]:
        self._pos_evt.clear()
        self._dev.get_positions()
        self._pos_evt.wait(timeout=1.0)   # sim replies synchronously
        with self._lock:
            out = {"alpha": self._alpha}
            if self._has_beta:
                out["beta"] = self._beta
            return out

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
