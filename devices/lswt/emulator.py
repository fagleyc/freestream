"""Simulated ABB ACS530 fan drive for hardware-free development.

Exposes the same surface as :class:`~lswt.drive.AbbAcs530`
(``connect/close/read_actual_hz/write_reference/write_control/start/
stop``) and models the plant: a first-order fan (time constant
``tau_s``, ~3 s like the real spool) that honors start/stop and the
reference magnitude.

The sim serves the actual-Hz register the way the drive plausibly
would under the negative-reference convention: the reported frequency
carries the SIGN of the last written reference (the driver takes
``abs()`` — exercising that path). Sign behaviour of the real register
is unverified live; the magnitude is the truth either way.

``last_control`` / ``last_reference`` (signed, as written after the
sign) / ``reference_writes`` are recorded for tests.
"""

from __future__ import annotations

import threading
import time
from typing import List, Optional

from .drive import (CMD_START, CMD_STOP, FULL_SPEED_HZ, REF_FULL_COUNTS,
                    LswtError)


def _signed16(v: int) -> int:
    return v - 0x1_0000 if v >= 0x8000 else v


class SimAcs530:
    """Physics-sim stand-in for AbbAcs530 (first-order fan model)."""

    def __init__(self, reference_sign: int = -1, tau_s: float = 3.0):
        self.reference_sign = 1 if reference_sign >= 0 else -1
        self.tau_s = float(tau_s)      # tests may shrink for speed
        self._connected = False
        self._running = False          # last control word was START
        self._ref_signed = 0           # signed reference as written
        self._hz = 0.0                 # plant output frequency magnitude
        self._t_last = time.perf_counter()
        self._lock = threading.Lock()
        # ── test hooks ──
        self.last_control: Optional[int] = None
        self.last_reference: Optional[int] = None      # signed
        self.reference_writes: List[int] = []          # signed history
        self.fail_reads = False        # raise LswtError from reads

    # ── connection surface ──
    def connect(self) -> None:
        self._connected = True
        self._t_last = time.perf_counter()

    def close(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ── plant ──
    def _advance(self) -> None:
        now = time.perf_counter()
        dt = min(now - self._t_last, 1.0)
        self._t_last = now
        target = 0.0
        if self._running:
            target = (abs(self._ref_signed) / REF_FULL_COUNTS *
                      FULL_SPEED_HZ)
        alpha = min(dt / max(self.tau_s, 1e-6), 1.0)
        self._hz += (target - self._hz) * alpha

    # ── protocol surface ──
    def read_actual_hz(self) -> float:
        with self._lock:
            if not self._connected:
                raise LswtError("not connected (sim)")
            if self.fail_reads:
                raise LswtError("simulated comm failure")
            self._advance()
            sign = -1.0 if self._ref_signed < 0 else 1.0
            return sign * round(self._hz * 10.0) / 10.0

    def write_reference(self, counts: int) -> None:
        counts = max(0, min(REF_FULL_COUNTS, int(counts)))
        value = self.reference_sign * counts
        with self._lock:
            if not self._connected:
                raise LswtError("not connected (sim)")
            self._advance()
            self._ref_signed = _signed16(value & 0xFFFF)
            self.last_reference = self._ref_signed
            self.reference_writes.append(self._ref_signed)

    def write_control(self, word: int) -> None:
        with self._lock:
            if not self._connected:
                raise LswtError("not connected (sim)")
            self._advance()
            self.last_control = int(word)
            if word == CMD_START:
                self._running = True
            elif word == CMD_STOP:
                self._running = False

    def start(self) -> None:
        self.write_control(CMD_START)

    def stop(self) -> None:
        self.write_control(CMD_STOP)
