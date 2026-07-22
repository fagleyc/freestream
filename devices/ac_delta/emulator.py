"""Simulated crescent axis physics for DLL-free/hardware-free development.

Each speed step maps to a slew rate; acceleration is first-order so
"snappiness" is visible in the sim too. The sim axis exposes the same
surface as :class:`~ac_delta.axis.CrescentAxis`.
"""

from __future__ import annotations

import threading
import time
from typing import Optional

from .axis import FWD_STEPS, REV_STEPS
from .config import AxisConfig

# deg/s per speed step 1..5 (plausible crescent slew rates)
STEP_RATES = [0.25, 0.5, 1.0, 2.0, 3.5]
ACCEL_TC = 0.15          # s, first-order response to commanded rate


class SimAxis:
    """Physics-sim stand-in for CrescentAxis."""

    def __init__(self, cfg: AxisConfig, timeout_s: float = 1.0,
                 start_angle: float = 0.0):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._angle = start_angle
        self._rate = 0.0            # current deg/s (angle space)
        self._cmd_rate = 0.0        # commanded deg/s (angle space)
        self._t_last = time.perf_counter()
        self._connected = False
        self._last_command: Optional[int] = None

    # ── connection surface ──
    def connect(self) -> None:
        self._connected = True
        self._t_last = time.perf_counter()

    def close(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ── physics ──
    def _advance(self) -> None:
        now = time.perf_counter()
        dt = min(now - self._t_last, 0.5)
        self._t_last = now
        # first-order rate response, then integrate angle
        alpha = min(dt / ACCEL_TC, 1.0)
        self._rate += (self._cmd_rate - self._rate) * alpha
        self._angle += self._rate * dt

    # ── protocol surface ──
    def read_encoder(self) -> int:
        with self._lock:
            self._advance()
            return self.cfg.angle_to_encoder(self._angle)

    def read_angle(self) -> float:
        return self.cfg.encoder_to_angle(self.read_encoder())

    def command_step(self, step: int, forward: bool) -> None:
        with self._lock:
            self._advance()
            rate = STEP_RATES[step - 1]
            self._cmd_rate = rate if forward else -rate
            table_forward = forward != self.cfg.invert_direction
            self._last_command = (FWD_STEPS if table_forward
                                  else REV_STEPS)[step - 1]

    def stop(self) -> None:
        with self._lock:
            self._advance()
            self._cmd_rate = 0.0
            self._rate = 0.0        # sim brake: immediate hold
            self._last_command = self.cfg.stop_value

    def last_command(self):
        return self._last_command
