"""Serial emulator for the Heise PM indicator — run the driver with
``force_sim=True`` and no hardware.

Duck-typed stand-in for ``serial.Serial``: answers the remote-protocol
command subset the driver uses. Left port simulates a pressure sensor
(ambient ~14.7 psi with slow wander + noise, converted per the active
EUNIT code); right port simulates an RTD (~72 °F, slow drift).
"""

from __future__ import annotations

import math
import random
import time
from typing import Optional

# psi → unit conversion factors (drawing 822B122 per Appendix A)
_CONVERT = {0: 1.0, 1: 27.703, 2: 2.03602, 3: 27.703, 4: 2.2457,
            5: 0.0689476, 6: 68.9476, 7: 6.89476, 8: 0.00689476,
            9: 51.7149, 10: 70.433, 11: 684.82, 12: 0.07030696}


class SimSerial:
    """Emulated indicator on a fake serial port."""

    def __init__(self, config=None):
        self._cfg = config
        self._buf = bytearray()
        self._rx = bytearray()
        self._t0 = time.time()
        #: EUNIT codes (left, right). Left is an RTD port (live bench):
        #: it reports its own temperature-unit code and REJECTS any
        #: attempt to write a different code (Err02, seen 2026-07-23).
        self._units = [15, 0]
        self._rtd_left_code = 15
        self._tare = [0, 0]
        self._zero_off = [0.0, 0.0]
        self._rng = random.Random(0x4E15E)

    # ── physics ──────────────────────────────────────────────────────────
    def _pressure_psi(self) -> float:
        t = time.time() - self._t0
        return (14.696 + 0.02 * math.sin(2 * math.pi * t / 60.0)
                + self._rng.gauss(0.0, 0.0005) - self._zero_off[0])

    def _temperature_f(self) -> float:
        t = time.time() - self._t0
        return (72.4 + 0.5 * math.sin(2 * math.pi * t / 600.0)
                + self._rng.gauss(0.0, 0.02))

    # ── command handling ─────────────────────────────────────────────────
    def _respond(self, text: str) -> None:
        # live wire truth (2026-07-23): CR-only EOM
        self._rx += (text + "\r").encode("ascii")

    def _handle(self, cmd: str) -> None:
        c = cmd.strip()
        u = c.upper()
        if c == "?":
            # bench layout (2026-07-23): left = RTD temperature,
            # right = pressure (EUNIT code of the right port applies)
            left = self._temperature_f()
            right = self._pressure_psi() * _CONVERT[self._units[1]]
            self._respond(f"{left:.6f},{right:.6f}")
        elif u.startswith("EUNIT?"):
            self._respond(f"{self._units[0]},{self._units[1]}")
        elif u.startswith("EUNIT"):
            vals = [int(v) for v in c[5:].replace(",", " ").split()]
            left = vals[0]
            right = vals[1] if len(vals) > 1 else vals[0]
            if left != self._rtd_left_code:
                self._respond("Err02")      # RTD port: code locked
            else:
                self._units = [left, right]
                self._respond("OK")
        elif u.startswith("ZERO"):
            self._zero_off[0] = self._pressure_psi() + self._zero_off[0]
            self._respond("OK")
        elif u.startswith("TARE?"):
            self._respond(f"{self._tare[0]},{self._tare[1]}")
        elif u.startswith("TARE"):
            vals = [int(v) for v in c[4:].replace(",", " ").split()]
            self._tare = [vals[0], vals[1] if len(vals) > 1 else 0]
            self._respond("OK")
        elif u.startswith("DAMP?"):
            self._respond("2")
        elif u.startswith("DAMP"):
            self._respond("OK")
        elif u.startswith("BATCK?"):
            self._respond("6.71")
        elif u.startswith("LASTERR?"):
            self._respond("None")
        elif u.startswith("MINMAX?"):
            self._respond("-0.012719,1.075889,-0.002789,0.018580")
        elif u.startswith("PORT?"):
            self._respond("2")
        elif u.startswith("PORT"):
            self._respond("OK")
        elif u.startswith("HOLD"):
            self._respond("OK" if not u.endswith("?") else "0")
        else:
            self._respond("Err01")

    # ── serial.Serial surface ────────────────────────────────────────────
    def write(self, data: bytes) -> int:
        self._buf += data
        while b"\r" in self._buf:
            line, _, rest = bytes(self._buf).partition(b"\r")
            self._buf = bytearray(rest)
            # live wire truth: the indicator ECHOES the command line,
            # then a bare CR, then the data ('?\r' + '\r' + '...\r')
            self._rx += line + b"\r\r"
            self._handle(line.decode("ascii", errors="replace"))
        return len(data)

    def read_until(self, expected: bytes = b"\n") -> bytes:
        i = self._rx.find(expected)
        if i < 0:
            return b""
        out = bytes(self._rx[:i + len(expected)])
        del self._rx[:i + len(expected)]
        return out

    def reset_input_buffer(self) -> None:
        self._rx.clear()

    def close(self) -> None:
        pass
