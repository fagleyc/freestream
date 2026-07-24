"""Simulated WAGO traverse PLC for hardware-free development.

Exposes the same surface as :class:`~traverse_swt.plc.WagoTraversePlc`
(``connect/close/read_block/read_status/write_control``) and models the
plant:

* three axes in velocity mode with first-order acceleration at the
  PLC program's FIXED speed (``SIM_RATE`` ≈ 2000 counts/s; the host has
  no speed control on the rig — tests may bump the per-instance
  ``sim_rate`` for fast convergence),
* an injectable stall hook (``stalled_axes``) that freezes an axis's
  counts to model a faulted/disabled 750-673 module,
* per-axis NEGATIVE limit switches with the RIG's polarity (verified
  2026-07-22): each enabled axis's StatusWord bit (%MW1 bit0/1/2) is
  HIGH when healthy and CLEARS when the plant reaches its negative
  travel end (``_SimAxis.neg_limit_counts``) — exactly what the
  host-side homing sequence and limit reaction poll. X's disabled limit
  input serves a dead 0. The module lockout is unlinked on the rig, so
  the sim keeps stepping past the switch until the HOST drops the jog.

The module's position counter is served as the rig's now does: an
UNSIGNED ring rolling over cleanly at ``wrap_modulus`` counts
(0…999,999 by default — 999999→0 and 0→999999). Physical travel is
unbounded, so long moves wrap the raw register any number of times;
the driver's unwrap keeps the absolute position.

The sim's plant truth follows each axis's ``fwd_increases_counts``
config (live-verified per axis), so the driver's direction mapping is
exercised against the same wiring sense as the rig.
"""

from __future__ import annotations

import threading
import time

from .config import TraverseConfig
from .plc import STATUS_LIMIT_MASK, BlockReading

SIM_RATE = 2_000.0        # counts/s — the PLC program's fixed speed
ACCEL_TC = 0.12           # s, first-order response to commanded rate

# Hard cap on the sim plant's per-tick counts advance. The huge-slope Z
# axis, scaled up by rate_scale (~66×) and a fast adapter sim_rate, would
# otherwise step ~80-100k counts/tick — brushing the driver's
# max_counts_per_tick jump guard (default 100k), which then misreads
# legitimate fast motion as a counter-reset and FREEZES the axis. Cap the
# per-tick delta well under that guard so a fast sim can never trip it
# (the axis just takes a few more ticks to cover a long move).
SIM_MAX_COUNTS_PER_TICK = 40_000.0

# default distance (counts) from the power-up zero to the negative
# travel end — where the sim asserts the axis's limit bit. Far enough
# that ordinary sim moves (a few inches at ~15k counts/in) don't touch
# it; tests move it (or bump SimPlc.sim_rate) to shape homing scenarios.
SIM_NEG_LIMIT_COUNTS = 60_000.0

# synthetic 750-673 S1 status codes (arbitrary sim values; the real
# module's byte layout is learned live from the Diagnostics log)
SIM_S1_IDLE = 0x21
SIM_S1_MOVING = 0x25
SIM_S1_FAULT = 0xA1


class _SimAxis:
    def __init__(self, cfg):
        self.cfg = cfg
        self.counts = 0.0          # TRUE (continuous) plant position
        self.rate = 0.0
        self.cmd_rate = 0.0
        # per-axis counts-rate scale: the rig's fixed step rate turns
        # into very different COUNTS/s per axis (gearing/encoder scale
        # ∝ the slope — live: Z ≈ 39k counts/s vs Y ≈ 15k). Without the
        # scale a nominal counts/s rate makes the huge-slope Z crawl at
        # ~1/60 the inch-rate of X/Y in sim. Nominal 15k counts/in keeps
        # X/Y at exactly 1.0 (unchanged behavior); only Z scales up.
        self.rate_scale = max(1.0, abs(cfg.clicks_per_inch) / 15_000.0)
        # plant truth, CAPTURED at construction (not read live from the
        # config): the driver's belief can then be flipped mid-session
        # without silently flipping the plant too — which is exactly the
        # mismatch the wrong-way trip exists to catch
        self.fwd_sign = 1.0 if cfg.fwd_increases_counts else -1.0
        # counts direction of DECREASING inches (negative travel):
        # d(inches)/d(counts) = 1/clicks_per_inch, so a negative slope
        # means +counts moves toward −inches
        self.neg_dir = 1.0 if cfg.clicks_per_inch < 0 else -1.0
        # The limit switch sits where the homing SEEK actually drives
        # the PLANT: the seek jogs the configured bit (home_jog_fwd →
        # fwd_mask, else rev_mask) and the plant's captured fwd_sign
        # maps that bit to a counts direction. Placing the switch on
        # that side guarantees a sim homing cycle always reaches it —
        # even in tests that deliberately flip the driver's beliefs.
        seek_dir = (self.fwd_sign
                    if getattr(cfg, "home_jog_fwd", True)
                    else -self.fwd_sign)
        self.seek_dir = seek_dir
        # switch distance scales with the slope so it sits "a few
        # inches" out on EVERY axis (fixed counts would put it 0.06"
        # from zero on the huge-slope Z)
        self.neg_limit_counts = (SIM_NEG_LIMIT_COUNTS * self.rate_scale
                                 * seek_dir)

    def advance(self, dt: float) -> None:
        alpha = min(dt / ACCEL_TC, 1.0)
        self.rate += (self.cmd_rate - self.rate) * alpha
        # cap the per-tick step so a fast sim never trips the driver's
        # counter-jump guard (see SIM_MAX_COUNTS_PER_TICK)
        delta = self.rate * dt
        if delta > SIM_MAX_COUNTS_PER_TICK:
            delta = SIM_MAX_COUNTS_PER_TICK
        elif delta < -SIM_MAX_COUNTS_PER_TICK:
            delta = -SIM_MAX_COUNTS_PER_TICK
        self.counts += delta

    @property
    def at_neg_limit(self) -> bool:
        """True at/past the switch (in the axis's seek direction)."""
        return (self.counts - self.neg_limit_counts) * self.seek_dir >= 0

    @property
    def raw(self) -> int:
        """What the PLC serves: the module counter on the UNSIGNED
        wrap_modulus ring (0…m−1, rolling over cleanly — the rig's
        reconfigured 1M rollover). True counts if wrap_modulus is 0.
        """
        v = int(round(self.counts))
        m = self.cfg.wrap_modulus
        return v % m if m else v


class SimPlc:
    """Physics-sim stand-in for WagoTraversePlc."""

    def __init__(self, config: TraverseConfig):
        self.config = config
        self._axes = {c.name: _SimAxis(c) for c in config.axes()}
        self._control = 0
        self._connected = False
        self._t_last = time.perf_counter()
        self._lock = threading.Lock()
        # axes listed here ignore commands (frozen counts) — models a
        # faulted/disabled stepper module for stall-detection tests
        self.stalled_axes: set = set()
        # the fixed plant speed (counts/s). The rig's PLC program fixes
        # ~2000 steps/s with no host control; tests bump this instance
        # attribute for fast convergence (it is NOT a config field).
        self.sim_rate: float = SIM_RATE

    # ── connection surface ──
    def connect(self) -> None:
        self._connected = True
        self._t_last = time.perf_counter()

    def close(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ── protocol surface ──
    def _advance(self) -> None:
        now = time.perf_counter()
        dt = min(now - self._t_last, 0.5)
        self._t_last = now
        for ax in self._axes.values():
            ax.advance(dt)

    def _status_word(self) -> int:
        """StatusWord %MW1 with the RIG's polarity (verified 2026-07-22):
        the NC chain drives an axis's bit HIGH when healthy and it
        CLEARS when the switch is engaged (active-low). Axes whose limit
        input is disabled on the rig (X) serve a dead 0 bit."""
        status = 0
        for n, a in self._axes.items():
            if a.cfg.limit_enabled and not a.at_neg_limit:
                status |= STATUS_LIMIT_MASK[n]
        return status

    def read_block(self) -> BlockReading:
        with self._lock:
            self._advance()
            counts = {n: a.raw for n, a in self._axes.items()}
            module_status = {}
            for n, a in self._axes.items():
                if n in self.stalled_axes:
                    s1 = SIM_S1_FAULT
                elif abs(a.cmd_rate) > 0:
                    s1 = SIM_S1_MOVING
                else:
                    s1 = SIM_S1_IDLE
                module_status[n] = (s1, 0x00, 0x00)
            return BlockReading(control=self._control,
                                status=self._status_word(),
                                counts=counts,
                                module_status=module_status)

    def read_status(self) -> int:
        with self._lock:
            self._advance()
            return self._status_word()

    def write_control(self, word: int, force: bool = False) -> None:
        with self._lock:
            self._advance()
            self._control = word & 0xFFFF
            for ax in self._axes.values():
                if ax.cfg.name in self.stalled_axes:
                    ax.cmd_rate = 0.0
                    ax.rate = 0.0
                    continue
                # fixed plant speed — the rig has no host speed control
                # (per-axis rate_scale: counts/s ∝ slope, see _SimAxis)
                rate = self.sim_rate * ax.rate_scale
                if word & ax.cfg.fwd_mask:
                    ax.cmd_rate = ax.fwd_sign * rate
                elif word & ax.cfg.rev_mask:
                    ax.cmd_rate = -ax.fwd_sign * rate
                else:
                    ax.cmd_rate = 0.0
                    ax.rate = 0.0          # sim brake: immediate hold

    def last_control(self):
        return self._control
