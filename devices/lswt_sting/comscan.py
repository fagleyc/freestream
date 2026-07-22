"""COM-port discovery and sting-chain probing.

Two layers:

* :func:`list_com_ports` — every serial port Windows knows about, with
  the human-readable description (so a "Prolific USB-to-Serial Comm
  Port (COM9)" is recognizable at a glance).
* :func:`probe_port` / :func:`search` — open a port at the sting's
  fixed 9600-8N1, send the chain probe ``1R`` and classify the answer:
  a line containing ``*R``/``*B``/``*S`` (or the command echo) means a
  sting indexer chain is listening there.

CLI:  ``python -m lswt_sting.comscan``  (or ``devices/probe_sting_com.py``)
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .protocol import BUSY, READY, STALLED

PROBE_CMD = b"1R\r"
STING_TOKENS = (READY, BUSY, STALLED)


@dataclass
class PortInfo:
    device: str                     # "COM9"
    description: str = ""           # "Prolific USB-to-Serial Comm Port"
    hwid: str = ""


@dataclass
class ProbeResult:
    port: PortInfo
    opened: bool = False
    response: str = ""              # raw text read back (repr-safe)
    is_sting: bool = False
    error: str = ""
    lines: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        d = (f" - {self.port.description}"
             if self.port.description else "")
        if not self.opened:
            return f"{self.port.device}{d}: cannot open ({self.error})"
        if self.is_sting:
            return (f"{self.port.device}{d}: STING CHAIN FOUND "
                    f"({self.response!r})")
        if self.response:
            return (f"{self.port.device}{d}: responds, but not a sting "
                    f"({self.response!r})")
        return f"{self.port.device}{d}: silent"


def list_com_ports() -> List[PortInfo]:
    """All serial ports visible to the OS, richest-info first."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    infos = [PortInfo(p.device, p.description or "", p.hwid or "")
             for p in list_ports.comports()]
    infos.sort(key=lambda p: (not p.description,
                              int("".join(filter(str.isdigit, p.device))
                                  or 0)))
    return infos


def probe_port(port: PortInfo, baud: int = 9600,
               timeout_s: float = 0.7,
               _serial_factory: Optional[Callable] = None) -> ProbeResult:
    """Open one port, send ``1R``, and classify whatever answers.

    Read-only with respect to the drives: ``R`` is a pure status query.
    ``_serial_factory(device, baud, timeout_s)`` is injectable for
    tests; the default opens a real ``serial.Serial``.
    """
    result = ProbeResult(port=port)
    if _serial_factory is None:
        def _serial_factory(device, baud, timeout_s):
            import serial
            sp = serial.Serial(port=device, baudrate=baud,
                               bytesize=serial.EIGHTBITS,
                               parity=serial.PARITY_NONE,
                               stopbits=serial.STOPBITS_ONE,
                               timeout=timeout_s,
                               write_timeout=timeout_s)
            try:
                sp.dtr = True
                sp.rts = True
            except Exception:               # noqa: BLE001
                pass
            return sp

    try:
        sp = _serial_factory(port.device, baud, timeout_s)
    except Exception as exc:                # noqa: BLE001
        result.error = str(exc)
        return result
    result.opened = True
    try:
        try:
            sp.reset_input_buffer()
        except Exception:                   # noqa: BLE001
            pass
        sp.write(PROBE_CMD)
        # echo line + status line, if anyone is listening
        for _ in range(2):
            raw = sp.read_until(b"\r")
            if not raw:
                break
            line = raw.rstrip(b"\r\n").decode("ascii", errors="replace")
            result.lines.append(line)
        result.response = " | ".join(result.lines)
        result.is_sting = any(
            tok in line for line in result.lines for tok in STING_TOKENS
        ) or any(line.strip().endswith("1R") for line in result.lines)
    except Exception as exc:                # noqa: BLE001
        result.error = str(exc)
    finally:
        try:
            sp.close()
        except Exception:                   # noqa: BLE001
            pass
    return result


def search(baud: int = 9600, timeout_s: float = 0.7,
           on_progress: Optional[Callable[[ProbeResult], None]] = None,
           _serial_factory: Optional[Callable] = None
           ) -> List[ProbeResult]:
    """Probe every visible COM port for a sting chain."""
    results = []
    for port in list_com_ports():
        r = probe_port(port, baud, timeout_s,
                       _serial_factory=_serial_factory)
        results.append(r)
        if on_progress:
            on_progress(r)
    return results


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Search COM ports for the LSWT sting indexer chain")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--timeout", type=float, default=0.7,
                        help="per-port read timeout [s]")
    args = parser.parse_args(argv)

    ports = list_com_ports()
    if not ports:
        print("No COM ports found (is pyserial installed?)")
        return 1
    print(f"Probing {len(ports)} port(s) at {args.baud} baud "
          f"with '1R'...\n")
    t0 = time.time()
    found = []
    for r in search(args.baud, args.timeout):
        print("  " + r.summary)
        if r.is_sting:
            found.append(r)
    print(f"\n{len(found)} sting chain(s) found in "
          f"{time.time() - t0:.1f} s")
    for r in found:
        print(f"  -> use com_port = \"{r.port.device}\"")
    return 0 if found else 2


if __name__ == "__main__":
    raise SystemExit(main())
