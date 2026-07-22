#!/usr/bin/env python
"""READ-ONLY probe of the SSWT traverse WAGO PLC (192.168.1.21).

Never writes a register. Three jobs:

1. Verify the register map live: dump %MW0..%MW15 (wire 12288..12303)
   raw, decode ControlWord / StatusWord / DINT positions, and compare
   FC3 (holding) vs FC4 (input) reads — the legacy C# used FC4 for
   positions; both should serve the same %MW image on a WAGO 750.
2. Verify DINT word order: for realistic positions the HIGH word of
   each pair is 0x0000 or 0xFFFF; if that pattern sits in the FIRST
   word instead, the PLC is serving high-word-first and plc.py's
   ``_dint`` needs swapping.
3. Monitor the limit switches: prints every StatusWord transition with
   a timestamp and the positions at that instant. Push each switch by
   hand (or run the axis to its stop USING THE OLD PANEL, not this
   script) and watch for the bit — dead, inverted, or chattering
   switches show up right here.

    python probe_traverse.py                # one dump, then monitor
    python probe_traverse.py --once         # single dump only
    python probe_traverse.py --ip 192.168.1.21 --interval 0.1
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from traverse_swt.plc import _dint, _mb_call  # noqa: E402

BLOCK_START = 12288
BLOCK_COUNT = 16
POS_WORD = {"X": 10, "Y": 12, "Z": 14}
LIMIT_BITS = {"X": 0, "Y": 1, "Z": 2}


def dump(client, unit_id: int) -> None:
    rr3 = _mb_call(client.read_holding_registers, BLOCK_START,
                   count=BLOCK_COUNT, unit_id=unit_id)
    if rr3.isError():
        print(f"FC3 block read FAILED: {rr3}")
        return
    regs = rr3.registers
    print(f"\nFC3 %MW0..%MW15 @ wire {BLOCK_START}:")
    for i, v in enumerate(regs):
        print(f"  %MW{i:<2} wire {BLOCK_START + i}  = {v:5d}  0x{v:04X}")

    control, status = regs[0], regs[1]
    print(f"\nControlWord = 0x{control:04X}   StatusWord = 0x{status:04X} "
          f"(bit0 X-limit={status & 1}, bit1 Y={status >> 1 & 1}, "
          f"bit2 Z={status >> 2 & 1})")
    for ax, w in POS_WORD.items():
        lo, hi = regs[w], regs[w + 1]
        lo_first = _dint(lo, hi)
        hi_first = _dint(hi, lo)
        mark = ""
        if hi in (0x0000, 0xFFFF) and lo not in (0x0000, 0xFFFF):
            mark = "  ← low-word-first confirmed"
        elif lo in (0x0000, 0xFFFF) and hi not in (0x0000, 0xFFFF):
            mark = "  ← looks HIGH-word-first — plc._dint needs swapping!"
        print(f"  {ax}: words ({lo:5d},{hi:5d})  low-first={lo_first:+d}  "
              f"(high-first would be {hi_first:+d}){mark}")

    rr4 = _mb_call(client.read_input_registers, BLOCK_START,
                   count=BLOCK_COUNT, unit_id=unit_id)
    if rr4.isError():
        print(f"\nFC4 read failed ({rr4}) — FC3 it is")
    else:
        same = rr4.registers == regs
        print(f"\nFC4 vs FC3: {'IDENTICAL' if same else 'DIFFER'}"
              + ("" if same else f"\n  FC4: {rr4.registers}"))

    # 750-673 module status bytes from the physical input image @ addr 0
    from traverse_swt.plc import decode_module_status
    mi = _mb_call(client.read_holding_registers, 0, count=18,
                  unit_id=unit_id)
    if mi.isError():
        print(f"\ninput image read @0 FAILED ({mi}) — the driver will "
              f"disable module status automatically")
    else:
        print("\n750-673 module status bytes (S1·S2·S3):")
        for ax, (s1, s2, s3) in decode_module_status(
                list(mi.registers)).items():
            print(f"  {ax}: {s1:02X}·{s2:02X}·{s3:02X}")
        print(f"  raw input words: {list(mi.registers)}")


def monitor(client, unit_id: int, interval: float) -> None:
    print(f"\nMonitoring StatusWord + positions every {interval}s "
          f"(Ctrl+C to stop). Trip the switches by hand and watch:")
    last_status = None
    last_print = 0.0
    while True:
        rr = _mb_call(client.read_holding_registers, BLOCK_START,
                      count=BLOCK_COUNT, unit_id=unit_id)
        now = time.time()
        stamp = time.strftime("%H:%M:%S", time.localtime(now)) + \
            f".{int(now % 1 * 10)}"
        if rr.isError():
            print(f"{stamp}  read error: {rr}")
            time.sleep(interval)
            continue
        regs = rr.registers
        status = regs[1]
        pos = {ax: _dint(regs[w], regs[w + 1])
               for ax, w in POS_WORD.items()}
        if status != last_status:
            desc = "  ".join(
                f"{ax}={'MADE' if status >> b & 1 else 'clear'}"
                for ax, b in LIMIT_BITS.items())
            print(f"{stamp}  STATUS 0x{status:04X}  {desc}  "
                  f"@ X{pos['X']:+d} Y{pos['Y']:+d} Z{pos['Z']:+d}")
            last_status = status
        elif now - last_print >= 5.0:
            print(f"{stamp}  status 0x{status:04X}  "
                  f"X{pos['X']:+d} Y{pos['Y']:+d} Z{pos['Z']:+d}")
            last_print = now
        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ip", default="192.168.1.21")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--unit", type=int, default=1)
    ap.add_argument("--interval", type=float, default=0.2,
                    help="monitor poll period, s")
    ap.add_argument("--once", action="store_true",
                    help="dump once and exit (no monitoring)")
    args = ap.parse_args()

    from pymodbus.client import ModbusTcpClient
    client = ModbusTcpClient(args.ip, port=args.port, timeout=2.0)
    if not client.connect():
        print(f"cannot connect to {args.ip}:{args.port}")
        return 1
    print(f"connected to {args.ip}:{args.port} (unit {args.unit}) — "
          f"READ-ONLY, no writes")
    try:
        dump(client, args.unit)
        if not args.once:
            monitor(client, args.unit, args.interval)
    except KeyboardInterrupt:
        print("\nstopped")
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
