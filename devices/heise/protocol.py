"""Line-level RS-232 binding for the Heise PM indicator remote protocol.

Query/response ASCII lines. Outgoing commands are CR-terminated (what a
terminal's Enter sends — the verification procedure in Appendix A is
written around exactly that); responses end with the indicator's
configured end-of-message character. This reader tolerates CR, LF or
CRLF and reassembles lines that straddle the read timeout (pyserial's
``read_until`` returns partial bytes on timeout — lesson learned live
on the sting drives).

Transport-injectable like the sting protocol: anything with ``write`` /
``read_until`` / ``reset_input_buffer`` / ``close``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import List, Optional

log = logging.getLogger(__name__)


class HeiseError(Exception):
    """Serial- or protocol-level failure."""


class HeiseProtocol:
    """Thread-safe command/response layer over one serial port."""

    def __init__(self, port: Optional[object] = None):
        self._sp = port
        self._lock = threading.RLock()

    # ── transport ────────────────────────────────────────────────────────
    @classmethod
    def open(cls, com_port: str, baud: int = 9600,
             timeout_s: float = 1.0) -> "HeiseProtocol":
        try:
            import serial
        except ImportError as exc:              # pragma: no cover
            raise HeiseError("pyserial is not installed — "
                             "pip install pyserial") from exc
        try:
            sp = serial.Serial(port=com_port, baudrate=baud,
                               bytesize=serial.EIGHTBITS,
                               parity=serial.PARITY_NONE,
                               stopbits=serial.STOPBITS_ONE,
                               timeout=timeout_s, write_timeout=timeout_s)
        except Exception as exc:
            raise HeiseError(f"Cannot open {com_port}: {exc}") from exc
        try:
            sp.dtr = True
            sp.rts = True
        except Exception:                       # noqa: BLE001
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

    def clear_input(self) -> None:
        with self._lock:
            if self._sp is not None:
                try:
                    self._sp.reset_input_buffer()
                except Exception:               # noqa: BLE001
                    pass

    def resync(self, settle_s: float = 0.1) -> None:
        """Wait for in-flight bytes to land, then drain."""
        with self._lock:
            time.sleep(settle_s)
            self.clear_input()

    # ── line primitives ──────────────────────────────────────────────────
    def _write_line(self, text: str) -> None:
        if self._sp is None:
            raise HeiseError("port not open")
        try:
            self._sp.write(text.encode("ascii") + b"\r")
        except Exception as exc:
            raise HeiseError(f"serial write failed: {exc}") from exc

    def _read_line(self) -> str:
        """One CR-terminated line (a trailing LF, if the EOM is CRLF,
        is stripped as a leftover on the NEXT line).

        Live 2026-07-23: the indicator's EOM is CR — waiting for LF
        times out and glues echo + blank + data into one blob
        ('?\\r\\r73.614870,11.430730'). Read to CR; may legitimately
        return an empty string for the bare-CR blank lines the
        indicator emits between echo and data.
        """
        if self._sp is None:
            raise HeiseError("port not open")
        buf = b""
        for _ in range(3):
            try:
                raw = self._sp.read_until(b"\r")
            except Exception as exc:
                raise HeiseError(f"serial read failed: {exc}") from exc
            buf += raw
            if buf.endswith(b"\r"):
                break
            if not raw:
                break
        if not buf:
            raise HeiseError("no response (timeout) — is the indicator "
                             "in REMOTE protocol and powered on?")
        text = buf.strip(b"\r\n \t").decode("ascii", errors="replace")
        if not buf.endswith(b"\r") and not text:
            raise HeiseError(f"incomplete response {buf!r}")
        return text

    # ── commands ─────────────────────────────────────────────────────────
    def query(self, cmd: str) -> str:
        """Send ``cmd`` and return the first REAL response line.

        The live indicator echoes the command line back before the
        data, and separates them with a bare CR — skip echoes and
        blank lines. Failures resync the line so leftovers never
        poison the next transaction.
        """
        with self._lock:
            self._write_line(cmd)
            try:
                resp = ""
                for _ in range(4):          # blanks/echo then payload
                    resp = self._read_line()
                    if resp and resp != cmd:
                        break
            except HeiseError:
                self.resync()
                raise
            if not resp or resp == cmd:
                self.resync()
                raise HeiseError(
                    f"no data after echo for {cmd!r}")
            if resp.lower().startswith("err"):
                raise HeiseError(f"indicator error for {cmd!r}: {resp}")
            return resp

    def command(self, cmd: str) -> None:
        """Send a setter and require the ``OK`` acknowledgement."""
        resp = self.query(cmd)
        if resp.strip().upper() != "OK":
            self.resync()
            raise HeiseError(f"unexpected reply for {cmd!r}: {resp!r}")

    # ── typed helpers (Appendix A command library) ───────────────────────
    def read_values(self) -> List[float]:
        """``?`` → current measurement(s), one float per active port."""
        resp = self.query("?")
        try:
            return [float(v) for v in resp.split(",") if v.strip()]
        except ValueError:
            raise HeiseError(f"unparseable measurement {resp!r}") from None

    def get_units(self) -> List[int]:
        """``EUNIT?`` → engineering-unit code per port."""
        resp = self.query("EUNIT?")
        try:
            return [int(v) for v in resp.split(",") if v.strip()]
        except ValueError:
            raise HeiseError(f"unparseable EUNIT reply {resp!r}") from None

    def set_units(self, left: int, right: int) -> None:
        self.command(f"EUNIT {left}, {right}")

    def zero(self, left: bool, right: bool) -> None:
        self.command(f"ZERO {int(left)}, {int(right)}")

    def set_tare(self, left: bool, right: bool) -> None:
        self.command(f"TARE {int(left)}, {int(right)}")

    def get_tare(self) -> List[int]:
        resp = self.query("TARE?")
        return [int(v) for v in resp.split(",") if v.strip()]

    def set_damping(self, level: int) -> None:
        self.command(f"DAMP {int(level)}")

    def battery(self) -> float:
        return float(self.query("BATCK?"))

    def last_error(self) -> str:
        return self.query("LASTERR?")

    def minmax(self) -> List[float]:
        resp = self.query("MINMAX?")
        return [float(v) for v in resp.split(",") if v.strip()]
