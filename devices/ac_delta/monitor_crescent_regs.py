#!/usr/bin/env python
"""READ-ONLY register monitor for the crescent drives (C2000 map).

Run this, then JOG THE CRESCENT FROM THE REDLION. It samples the command
and encoder registers plus the C2000 status area on both drives and then
reports every change with timestamps — revealing exactly what the panel
writes to start/stop motion, and proving the encoder tracks.

    python monitor_crescent_regs.py --seconds 45
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pymodbus.client import ModbusTcpClient

# (label, wire address)
REGS = [
    ("cmd 8192", 8192),
    ("cmd 8193", 8193),
    ("encoder 8713", 8713),
    ("status 0x2101", 0x2101),
    ("freqcmd 0x2102", 0x2102),
    ("outfreq 0x2103", 0x2103),
    ("current 0x2104", 0x2104),
]


def mb_read1(client, addr, unit=1):
    for kw in ({"device_id": unit}, {"slave": unit}, {}):
        try:
            return client.read_holding_registers(addr, count=1, **kw)
        except TypeError:
            continue
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=45.0)
    args = ap.parse_args()

    clients = {}
    for name, ip in (("Alpha", "192.168.1.11"), ("Beta", "192.168.1.12")):
        c = ModbusTcpClient(ip, port=502, timeout=2.0)
        if c.connect():
            clients[name] = c
            print(f"{name} connected ({ip})")
        else:
            print(f"{name}: cannot connect ({ip})")
    if not clients:
        return 1

    print(f"\nMonitoring for {args.seconds:.0f} s — JOG FROM THE REDLION "
          f"NOW (both directions if possible)...")
    last = {(n, lbl): None for n in clients for lbl, _a in REGS}
    events = []
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < args.seconds:
        for name, c in clients.items():
            for lbl, addr in REGS:
                rr = mb_read1(c, addr)
                if rr is None or rr.isError():
                    continue
                v = rr.registers[0]
                key = (name, lbl)
                if last[key] is None:
                    last[key] = v
                    s = v - 65536 if v >= 32768 else v
                    print(f"  start {name} {lbl:15s} = {v} "
                          f"[0x{v:04X}]{f' ({s})' if s != v else ''}")
                elif v != last[key]:
                    stamp = time.perf_counter() - t0
                    s = v - 65536 if v >= 32768 else v
                    events.append((stamp, name, lbl, last[key], v))
                    print(f"  {stamp:7.2f}s {name} {lbl:15s} "
                          f"{last[key]} -> {v} [0x{v:04X}]"
                          f"{f' ({s})' if s != v else ''}")
                    last[key] = v
        time.sleep(0.08)

    print(f"\n{len(events)} change events captured.")
    for c in clients.values():
        c.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
