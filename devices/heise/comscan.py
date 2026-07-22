"""COM-port discovery and Heise-indicator probing.

Probes each port with the remote-protocol ``?`` query (read-only) and
classifies the answer: one or two comma-separated numbers means a Heise
PM indicator in REMOTE mode is listening. All supported baud rates are
tried (9600 first — the recommended setting).

CLI:  ``python -m heise.comscan``  (or ``devices/probe_heise_com.py``)
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from .config import BAUD_RATES

_NUMBERS = re.compile(
    r"^\s*[+-]?\d+(\.\d+)?\s*(,\s*[+-]?\d+(\.\d+)?\s*)?$")


@dataclass
class PortInfo:
    device: str
    description: str = ""
    hwid: str = ""


@dataclass
class ProbeResult:
    port: PortInfo
    opened: bool = False
    baud: int = 0
    response: str = ""
    is_heise: bool = False
    error: str = ""
    lines: List[str] = field(default_factory=list)

    @property
    def summary(self) -> str:
        d = (f" - {self.port.description}"
             if self.port.description else "")
        if not self.opened:
            return f"{self.port.device}{d}: cannot open ({self.error})"
        if self.is_heise:
            return (f"{self.port.device}{d}: HEISE INDICATOR FOUND at "
                    f"{self.baud} baud ({self.response!r})")
        if self.response:
            return (f"{self.port.device}{d}: responds, but not a Heise "
                    f"({self.response!r})")
        return f"{self.port.device}{d}: silent"


def list_com_ports() -> List[PortInfo]:
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


def _default_factory(device, baud, timeout_s):
    import serial
    sp = serial.Serial(port=device, baudrate=baud,
                       bytesize=serial.EIGHTBITS,
                       parity=serial.PARITY_NONE,
                       stopbits=serial.STOPBITS_ONE,
                       timeout=timeout_s, write_timeout=timeout_s)
    try:
        sp.dtr = True
        sp.rts = True
    except Exception:                       # noqa: BLE001
        pass
    return sp


def probe_port(port: PortInfo, bauds=(9600,), timeout_s: float = 0.8,
               _serial_factory: Optional[Callable] = None) -> ProbeResult:
    """Open one port and try ``?`` at each baud rate (read-only)."""
    result = ProbeResult(port=port)
    factory = _serial_factory or _default_factory
    for baud in bauds:
        try:
            sp = factory(port.device, baud, timeout_s)
        except Exception as exc:            # noqa: BLE001
            result.error = str(exc)
            return result
        result.opened = True
        try:
            try:
                sp.reset_input_buffer()
            except Exception:               # noqa: BLE001
                pass
            sp.write(b"?\r")
            lines = []
            for _ in range(2):
                raw = sp.read_until(b"\n")
                if not raw:
                    break
                line = raw.strip(b"\r\n \t").decode("ascii",
                                                    errors="replace")
                if line:
                    lines.append(line)
            if lines:
                result.lines = lines
                result.response = " | ".join(lines)
                result.baud = baud
                result.is_heise = any(_NUMBERS.match(ln) for ln in lines)
        except Exception as exc:            # noqa: BLE001
            result.error = str(exc)
        finally:
            try:
                sp.close()
            except Exception:               # noqa: BLE001
                pass
        if result.is_heise:
            break
    return result


def search(bauds=(9600,), timeout_s: float = 0.8,
           on_progress: Optional[Callable[[ProbeResult], None]] = None,
           _serial_factory: Optional[Callable] = None
           ) -> List[ProbeResult]:
    """Probe every visible COM port for a Heise indicator."""
    results = []
    for port in list_com_ports():
        r = probe_port(port, bauds, timeout_s,
                       _serial_factory=_serial_factory)
        results.append(r)
        if on_progress:
            on_progress(r)
    return results


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(
        description="Search COM ports for a Heise PM indicator "
                    "(remote protocol '?')")
    parser.add_argument("--all-bauds", action="store_true",
                        help="try every supported baud rate "
                             "(default: 9600 only)")
    parser.add_argument("--timeout", type=float, default=0.8)
    args = parser.parse_args(argv)

    bauds = tuple(sorted(BAUD_RATES, reverse=True)) \
        if args.all_bauds else (9600,)
    ports = list_com_ports()
    if not ports:
        print("No COM ports found (is pyserial installed?)")
        return 1
    print(f"Probing {len(ports)} port(s) with '?' at "
          f"{'/'.join(map(str, bauds))} baud...\n")
    t0 = time.time()
    found = []
    for r in search(bauds, args.timeout):
        print("  " + r.summary)
        if r.is_heise:
            found.append(r)
    print(f"\n{len(found)} indicator(s) found in {time.time() - t0:.1f} s")
    for r in found:
        print(f"  -> use com_port = \"{r.port.device}\", "
              f"baud = {r.baud}")
    return 0 if found else 2


if __name__ == "__main__":
    raise SystemExit(main())
