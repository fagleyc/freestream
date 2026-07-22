"""Wire-level emulator for the LSWT sting indexers.

``SimSerial`` mimics the two daisy-chained drives at the byte level (echo
line + response lines, CR terminators), so the whole stack — protocol
parser included — runs unmodified in sim mode. Motion is integrated in
real time at the drive velocity; ``inject_stall`` forces a ``*S`` status
to exercise the fault path in tests.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Dict, Optional

from .config import StingConfig


class _SimAxis:
    def __init__(self, unit: str, steps_per_s: float):
        self.unit = unit
        self.steps_per_s = steps_per_s
        self.counts = 0.0
        self.target = 0.0
        self.moving = False
        self.pending_distance = 0
        self.t_last = time.monotonic()

    def advance(self) -> None:
        now = time.monotonic()
        dt = now - self.t_last
        self.t_last = now
        if not self.moving:
            return
        step = self.steps_per_s * dt
        delta = self.target - self.counts
        if abs(delta) <= step:
            self.counts = self.target
            self.moving = False
        else:
            self.counts += step if delta > 0 else -step


class SimSerial:
    """Duck-typed stand-in for ``serial.Serial`` speaking the drive
    protocol."""

    def __init__(self, config: Optional[StingConfig] = None):
        cfg = config or StingConfig()
        self._axes: Dict[str, _SimAxis] = {}
        for ax in cfg.axes():
            try:
                v = float(ax.velocity)
            except ValueError:
                v = 0.1
            self._axes[ax.unit] = _SimAxis(ax.unit, v * ax.steps_per_rev)
        self._out: deque = deque()      # queued response bytes (lines)
        self.inject_stall: Optional[str] = None   # unit to report *S
        self.is_open = True

    # ── serial.Serial surface ────────────────────────────────────────────
    def write(self, data: bytes) -> int:
        line = data.rstrip(b"\r\n").decode("ascii", errors="replace")
        self._handle(line)
        return len(data)

    def read_until(self, expected: bytes = b"\r") -> bytes:
        if not self._out:
            return b""                  # timeout
        return self._out.popleft()

    def reset_input_buffer(self) -> None:
        self._out.clear()

    def close(self) -> None:
        self.is_open = False

    # ── protocol behaviour ───────────────────────────────────────────────
    def _reply(self, text: str) -> None:
        self._out.append(text.encode("ascii") + b"\r")

    def _handle(self, line: str) -> None:
        for ax in self._axes.values():
            ax.advance()
        self._reply(line)               # drives echo every command line

        unit, cmd = "", line
        if line[:1] in self._axes:
            unit, cmd = line[:1], line[1:]
        ax = self._axes.get(unit)

        if cmd == "R" and ax is not None:
            if self.inject_stall == unit:
                self._reply("*S")
            elif ax.moving:
                self._reply("*B")
            else:
                self._reply("*R")
        elif cmd == "PR" and ax is not None:
            n = int(round(ax.counts))
            self._reply(f"*{'+' if n >= 0 else '-'}{abs(n):08d}")
        elif cmd == "PZ" and ax is not None:
            ax.counts = 0.0
            ax.target = 0.0
        elif cmd.startswith("D") and ax is not None:
            try:
                ax.pending_distance = int(cmd[1:])
            except ValueError:
                pass
        elif cmd == "G" and ax is not None:
            ax.target = ax.counts + ax.pending_distance
            ax.moving = True
        elif cmd == "S" and ax is not None:
            ax.target = ax.counts
            ax.moving = False
        elif cmd == "Z" and ax is not None:
            ax.moving = False
        # A/AD/V/LD/SSA/SSI/FSD and broadcasts: echo only

    # ── test helpers ─────────────────────────────────────────────────────
    def axis_counts(self, unit: str) -> int:
        self._axes[unit].advance()
        return int(round(self._axes[unit].counts))
