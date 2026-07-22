#!/usr/bin/env python
"""Write-path diagnostic for the tunnel gateway (192.168.1.50).

Answers ONE question with the least-consequential write possible:
does the Red Lion's Modbus slave accept writes to Block2, and with
which function code?

Method: read the retained RPM_Set from Block1, then write THE SAME
value back to Block2's RPM_Set element (address 208) — a no-op for the
plant (same setpoint, fan state untouched, no button pulses). Tries
FC16 (write multiple registers) and, if rejected, FC6 singles, and
reports exactly what the slave said. Re-reads Block1 afterwards to
confirm the echo.

Without ``--arm`` it only reads and tells you what it WOULD write.

    python tunnel_write_check.py            # read-only dry run
    python tunnel_write_check.py --arm      # perform the same-value write
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tunnel_plc.gateway import _describe, _mb_call     # noqa: E402
from tunnel_plc.registers import (                     # noqa: E402
    BLOCK1_ADDR, BLOCK2_ADDR, decode_u32, encode_u32)

WORD_ORDER = "low_first"        # live-verified 2026-07-07


def read_rpm_set(client, unit):
    rr = _mb_call(client.read_holding_registers, BLOCK1_ADDR, count=2,
                  unit_id=unit)
    if rr.isError():
        raise SystemExit(f"Block1 read failed: {_describe(rr)}")
    return decode_u32(rr.registers[0], rr.registers[1], WORD_ORDER)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ip", default="192.168.1.50")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--unit", type=int, default=1)
    ap.add_argument("--arm", action="store_true",
                    help="actually perform the same-value write")
    args = ap.parse_args()

    from pymodbus.client import ModbusTcpClient
    client = ModbusTcpClient(args.ip, port=args.port, timeout=3.0)
    if not client.connect():
        print(f"cannot connect to {args.ip}:{args.port}")
        return 1
    try:
        raw = read_rpm_set(client, args.unit)
        addr = BLOCK2_ADDR["RPM_Set"]
        pair = list(encode_u32(raw, WORD_ORDER))
        print(f"retained RPM_Set (Block1) = raw {raw} "
              f"({raw * 0.1:g} RPM on the HMI)")
        print(f"plan: write the SAME raw value {raw} to Block2 RPM_Set "
              f"@ addr {addr} (registers {pair}) — a plant no-op")
        if not args.arm:
            print("\nDRY RUN (no writes). Re-run with --arm to test the "
                  "write path.")
            return 0

        ok_fc = None
        rr = _mb_call(client.write_registers, addr, values=pair,
                      unit_id=args.unit)
        if rr.isError():
            print(f"FC16 write multiple : REJECTED — {_describe(rr)}")
        else:
            print("FC16 write multiple : ACCEPTED")
            ok_fc = 16
        if ok_fc is None:
            fc6_ok = True
            for off, v in enumerate(pair):
                rr = _mb_call(client.write_register, addr + off, value=v,
                              unit_id=args.unit)
                if rr.isError():
                    print(f"FC6 single @ {addr + off} : REJECTED — "
                          f"{_describe(rr)}")
                    fc6_ok = False
                    break
                print(f"FC6 single @ {addr + off} = {v} : ACCEPTED")
            if fc6_ok:
                ok_fc = 6

        back = read_rpm_set(client, args.unit)
        print(f"\nBlock1 RPM_Set after: raw {back} "
              f"({'unchanged — good' if back == raw else 'CHANGED?!'})")
        if ok_fc:
            print(f"\nRESULT: writes WORK via FC{ok_fc}. The driver "
                  f"handles both automatically.")
        else:
            print("\nRESULT: the slave rejects ALL write functions on "
                  "Block2. Fix in Crimson 3 on the SSWT_Logger "
                  "database:\n"
                  "  1. Communications → the Modbus TCP slave → make "
                  "sure 'Read Only' is NO;\n"
                  "  2. the Block2 gateway block (elements 101-105) "
                  "must have direction 'Block to Tag'\n"
                  "     (master writes registers → tags); Block1 stays "
                  "'Tag to Block';\n"
                  "  3. re-download to the G315, then re-run this "
                  "check.")
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
