#!/usr/bin/env python
"""READ-ONLY probe of the SSWT tunnel Red Lion G315 (192.168.1.50).

Never writes a register. Jobs:

1. Dump Block1 (protocol address 0, 32 registers = 16 L4 elements) raw
   and decoded under BOTH word orders.
2. Determine the 32-bit word order empirically: any boolean that reads
   exactly 1 puts its bit in the LOW word (regs (1,0) → low_first;
   (0,1) → high_first). RPM works too if the fan is turning or a
   setpoint is retained. If every element is 0 the order stays
   undetermined — rerun with a light on.
3. Plausibility-check idle values: Actual_RPM ≈ 0, booleans ∈ {0, 1}.
4. Optionally monitor (default): prints every status change.

    python probe_tunnel.py                 # dump + monitor
    python probe_tunnel.py --once          # single dump
    python probe_tunnel.py --ip 192.168.1.50 --interval 0.5
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):      # Windows cp1252 console vs "←"
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tunnel_plc.gateway import _mb_call            # noqa: E402
from tunnel_plc.registers import (                 # noqa: E402
    BLOCK1_ADDR, BLOCK1_REGISTERS, BLOCK1_TAGS, decode_u32)


def dump(client, unit_id: int):
    rr = _mb_call(client.read_holding_registers, BLOCK1_ADDR,
                  count=BLOCK1_REGISTERS, unit_id=unit_id)
    if rr.isError():
        print(f"Block1 read FAILED: {rr}")
        return None
    regs = list(rr.registers)
    print(f"\nBlock1 raw ({BLOCK1_REGISTERS} registers @ addr "
          f"{BLOCK1_ADDR}):")
    votes = {"low_first": 0, "high_first": 0}
    for i, (tag, _attr, is_bool) in enumerate(BLOCK1_TAGS):
        lo, hi = regs[2 * i], regs[2 * i + 1]
        v_lf = decode_u32(lo, hi, "low_first")
        v_hf = decode_u32(lo, hi, "high_first")
        mark = ""
        if (lo, hi) != (0, 0):
            if hi == 0 and lo != 0:
                votes["low_first"] += 1
                mark = " ← nonzero LOW word"
            elif lo == 0 and hi != 0:
                votes["high_first"] += 1
                mark = " ← nonzero HIGH word"
        flag = " [bool]" if is_bool else ""
        print(f"  el{i + 1:>3} @{2 * i:>3}  ({lo:5d},{hi:5d})  "
              f"low_first={v_lf:<8d} high_first={v_hf:<11d} "
              f"{tag}{flag}{mark}")

    print("\nWord-order votes:", votes)
    if votes["low_first"] > votes["high_first"] == 0:
        print("  → LOW_FIRST confirmed. Set word_order='low_first' and "
              "word_order_verified=true.")
    elif votes["high_first"] > votes["low_first"] == 0:
        print("  → HIGH_FIRST confirmed. Set word_order='high_first' and "
              "word_order_verified=true.")
    else:
        print("  → UNDETERMINED (all elements zero or mixed) — rerun "
              "with the console on / a light lit.")

    # plausibility at idle
    order = "low_first" if votes["low_first"] >= votes["high_first"] \
        else "high_first"
    rpm = decode_u32(regs[2], regs[3], order)
    bools_ok = all(
        decode_u32(regs[2 * i], regs[2 * i + 1], order) in (0, 1)
        for i, (_t, _a, b) in enumerate(BLOCK1_TAGS) if b)
    print(f"\nPlausibility ({order}): Actual_RPM = {rpm} "
          f"({'plausible idle' if abs(rpm) < 50 else 'FAN TURNING?'}), "
          f"booleans all 0/1: {bools_ok}")
    return regs


def monitor(client, unit_id: int, interval: float):
    print(f"\nMonitoring Block1 every {interval}s (Ctrl+C to stop):")
    last = None
    while True:
        rr = _mb_call(client.read_holding_registers, BLOCK1_ADDR,
                      count=BLOCK1_REGISTERS, unit_id=unit_id)
        stamp = time.strftime("%H:%M:%S")
        if rr.isError():
            print(f"{stamp}  read error: {rr}")
        else:
            regs = tuple(rr.registers)
            if regs != last:
                changed = [
                    f"{BLOCK1_TAGS[i][0]}=({regs[2 * i]},{regs[2 * i + 1]})"
                    for i in range(len(BLOCK1_TAGS))
                    if last is None or
                    (regs[2 * i], regs[2 * i + 1]) !=
                    (last[2 * i], last[2 * i + 1])]
                print(f"{stamp}  " + "  ".join(changed))
                last = regs
        time.sleep(interval)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ip", default="192.168.1.50")
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--unit", type=int, default=1)
    ap.add_argument("--interval", type=float, default=0.5)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    from pymodbus.client import ModbusTcpClient
    client = ModbusTcpClient(args.ip, port=args.port, timeout=3.0)
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
