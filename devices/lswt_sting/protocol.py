"""Serial protocol binding for the LSWT sting indexers.

Wire format (recovered from the deployed C# ``HwControllerStingLSWT``):

* 9600 baud, 8N1, no handshake; lines terminated with a bare CR (``\\r``);
  500 ms read/write timeouts.
* Commands are ``<unit><cmd>\\r`` — unit ``1`` = Alpha, ``2`` = Beta, empty
  unit = broadcast (``SSI1``, ``FSD1``).
* The drives echo every command line; the echo is read back and validated
  after each write (an unexpected echo means a wiring/addressing problem).
* Query commands return one additional line:
  - ``R``  → ``*B`` busy | ``*R`` ready | ``*S`` stalled
  - ``PR`` → line containing the signed step counter (regex ``[+-]?\\d+``)

Command set used: ``A`` accel, ``AD`` decel, ``V`` velocity, ``D<steps>``
relative distance, ``G`` go, ``S`` stop, ``Z`` reset, ``PZ`` zero counter,
``PR`` position report, ``R`` status, ``LD3`` disable limit inputs,
``SSA0``/``SSI1``/``FSD1`` interface setup (sent verbatim as the legacy
tool does).

The class is transport-injectable: pass any object with ``write``/
``read_until``/``reset_input_buffer``/``close`` (e.g. the emulator's
``SimSerial``) instead of a real ``serial.Serial``.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Optional

log = logging.getLogger(__name__)

_POSITION_RE = re.compile(rb"[+-]?[0-9]+")

# status responses
READY = "*R"
BUSY = "*B"
STALLED = "*S"


class StingError(Exception):
    """Serial-level or protocol-level failure."""


class StingProtocol:
    """Thread-safe command/response layer over one RS-232 daisy chain."""

    def __init__(self, port: Optional[object] = None):
        self._sp = port                 # serial.Serial or SimSerial
        self._lock = threading.RLock()

    # ── transport ────────────────────────────────────────────────────────
    @classmethod
    def open(cls, com_port: str, baud: int = 9600,
             timeout_s: float = 0.5) -> "StingProtocol":
        try:
            import serial
        except ImportError as exc:              # pragma: no cover
            raise StingError("pyserial is not installed — "
                             "pip install pyserial") from exc
        try:
            sp = serial.Serial(port=com_port, baudrate=baud,
                               bytesize=serial.EIGHTBITS,
                               parity=serial.PARITY_NONE,
                               stopbits=serial.STOPBITS_ONE,
                               timeout=timeout_s, write_timeout=timeout_s)
        except Exception as exc:
            raise StingError(f"Cannot open {com_port}: {exc}") from exc
        # assert the handshake lines: some RS-232 level shifters (and a
        # few Prolific USB adapters) sit dead until DTR/RTS are high
        try:
            sp.dtr = True
            sp.rts = True
        except Exception:                   # noqa: BLE001
            pass
        return cls(sp)

    @property
    def is_open(self) -> bool:
        return self._sp is not None

    def close(self) -> None:
        with self._lock:
            if self._sp is not None:
                try:
                    self._sp.close()
                except Exception:               # noqa: BLE001
                    pass
                self._sp = None

    # ── line primitives ──────────────────────────────────────────────────
    def _write_line(self, text: str) -> None:
        if self._sp is None:
            raise StingError("port not open")
        try:
            self._sp.write(text.encode("ascii") + b"\r")
        except Exception as exc:
            raise StingError(f"serial write failed: {exc}") from exc

    def _read_line(self) -> str:
        """Read one COMPLETE CR-terminated line.

        ``read_until`` returns whatever partial bytes it has when the
        timeout expires — a response that straddles the timeout (seen
        live: '*+0' / '000000000' fragments while a drive was stepping)
        must not be taken for a full line, or every transaction after
        it reads the previous one's leftovers. Accumulate until the CR
        arrives, allowing a couple of extra timeout periods.
        """
        if self._sp is None:
            raise StingError("port not open")
        buf = b""
        for _ in range(3):
            try:
                raw = self._sp.read_until(b"\r")
            except Exception as exc:
                raise StingError(f"serial read failed: {exc}") from exc
            buf += raw
            if buf.endswith(b"\r"):
                break
            if not raw:                 # a whole timeout with nothing new
                break
        if not buf:
            raise StingError("no response (timeout) — is power on?")
        if not buf.endswith(b"\r"):
            raise StingError(f"incomplete response {buf!r} (timeout "
                             f"mid-line)")
        # drives terminate lines CR(+LF); a leftover LF from the previous
        # line must not pollute this one (legacy ReadLine tolerated it)
        return buf.strip(b"\r\n").decode("ascii", errors="replace")

    def clear_input(self) -> None:
        with self._lock:
            if self._sp is not None:
                try:
                    self._sp.reset_input_buffer()
                except Exception:               # noqa: BLE001
                    pass

    def resync(self, settle_s: float = 0.1) -> None:
        """Recover line discipline after a failed transaction.

        Clearing immediately is not enough: the line that caused the
        failure may still be in flight (seen live: a '2PZ' issued right
        after a glitched poll read the poll's late '2PR' echo). Wait
        for stragglers to land, then drain.
        """
        with self._lock:
            time.sleep(settle_s)
            self.clear_input()

    # ── protocol commands ────────────────────────────────────────────────
    def command(self, unit: str, cmd: str) -> None:
        """Send ``<unit><cmd>`` and validate the echoed line.

        One stale line (a leftover from an earlier glitched transaction)
        is absorbed before declaring a mismatch — the legacy tool's
        Contains-check-and-continue behaved the same way. Any failure
        resyncs the line so the NEXT transaction starts clean.
        """
        text = f"{unit}{cmd}"
        with self._lock:
            self._write_line(text)
            try:
                echo = self._read_line()
                if cmd not in echo:
                    log.debug("stale line %r before echo of %r — "
                              "absorbing", echo, text)
                    stale, echo = echo, self._read_line()
                    if cmd not in echo:
                        raise StingError(
                            f"unexpected echo for {text!r}: {stale!r} "
                            f"then {echo!r}")
            except StingError:
                self.resync()
                raise

    def command_blind(self, unit: str, cmd: str,
                      settle_s: float = 0.15) -> None:
        """Send ``<unit><cmd>`` without echo validation.

        The interface-setup software switches (``SSA``/``SSI``/``FSD``)
        and the ``Z`` reset alter the drives' serial/echo behaviour as
        they execute, so the echo for these commands (or the one after
        them) can legitimately never arrive — seen live on COM9 via a
        Prolific adapter: ``1R`` and ``SSI1`` answered, then ``SSA0``'s
        echo timed out. The legacy C# tool sends this bring-up stream
        without validating echoes; mirror that here: write the same
        bytes, give the drive a moment, then drain whatever did echo.
        """
        with self._lock:
            self._write_line(f"{unit}{cmd}")
            time.sleep(settle_s)
            self.clear_input()

    def query(self, unit: str, cmd: str) -> str:
        """Send ``<unit><cmd>`` and return the response line after the
        echo."""
        with self._lock:
            self.command(unit, cmd)
            try:
                return self._read_line()
            except StingError:
                self.resync()           # response lost — don't let it
                raise                   # surface as the next echo

    def stop_now(self, unit: str) -> None:
        """Immediate stop — raw write, no echo wait (E-stop path)."""
        with self._lock:
            self._write_line(f"{unit}S")

    def stop_all_now(self, units) -> None:
        """E-stop several units and drain the echoes atomically.

        Holds the protocol lock across write+drain so a concurrent poll
        transaction can never read the stop echoes as its own response.
        """
        import time as _time
        with self._lock:
            for unit in units:
                self._write_line(f"{unit}S")
            _time.sleep(0.1)
            if self._sp is not None:
                try:
                    self._sp.reset_input_buffer()
                except Exception:       # noqa: BLE001
                    pass

    # ── typed helpers ────────────────────────────────────────────────────
    def status(self, unit: str) -> str:
        """``R`` request → READY | BUSY | STALLED (raw line on surprise)."""
        resp = self.query(unit, "R")
        for token in (BUSY, READY, STALLED):
            if token in resp:
                return token
        return resp

    def position(self, unit: str) -> int:
        """``PR`` position report → signed step counter."""
        resp = self.query(unit, "PR")
        m = _POSITION_RE.search(resp.encode("ascii", errors="replace"))
        if not m:
            raise StingError(f"invalid position response: {resp!r}")
        return int(m.group())

    def zero_position(self, unit: str) -> None:
        self.command(unit, "PZ")

    def move_steps(self, unit: str, steps: int) -> None:
        """Load a relative distance and start the move (``D`` + ``G``)."""
        with self._lock:
            self.command(unit, f"D{int(steps)}")
            self.command(unit, "G")
