#!/usr/bin/env python
"""Extended live verification of the DaqBook driver (read-only).

Acquires for N seconds against the real device and checks the things the
short probe can't: sustained rate vs the ADC clock, monotonic timebase,
no buffer overruns, and stable channel statistics.

    python verify_daqbook_live.py [--seconds 20] [--rate 1000]
"""

import argparse
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daqbook_2000.config import DaqbookConfig
from daqbook_2000.device import Daqbook2000


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--rate", type=float, default=1000.0)
    ap.add_argument("--device", default="DaqBook2005")
    args = ap.parse_args()

    cfg = DaqbookConfig(device_name=args.device, scan_hz=args.rate)
    dev = Daqbook2000(cfg)
    overruns = []
    dev.on_status = lambda s: (overruns.append(s) if "overrun" in s.lower()
                               else print(f"[status] {s}"))
    ok = True
    try:
        dev.connect()
        dev.start()
        print(f"Requested {args.rate:.0f} Hz, ADC clock reports "
              f"{dev.actual_hz:.2f} Hz")
        t0 = time.perf_counter()
        n0 = dev.frame_count()
        time.sleep(args.seconds)
        elapsed = time.perf_counter() - t0
        n1 = dev.frame_count()
        meas = (n1 - n0) / elapsed
        print(f"\nScans: {n1 - n0} in {elapsed:.2f} s  ->  {meas:.2f} scans/s")
        drift = abs(meas - dev.actual_hz) / dev.actual_hz
        print(f"Rate vs ADC clock: {drift * 100:.3f}% "
              f"({'OK' if drift < 0.01 else 'CHECK'})")
        ok &= drift < 0.01

        tail = dev.ring.tail(int(min(args.seconds, 10) * dev.actual_hz))
        dt = np.diff(tail["t"])
        print(f"Timebase: median dt {np.median(dt) * 1e3:.4f} ms, "
              f"monotonic: {bool((dt > 0).all())}")
        ok &= bool((dt > 0).all())

        print(f"Overruns: {len(overruns)} "
              f"({'OK' if not overruns else 'CHECK poll_ms/buffer'})")
        ok &= not overruns

        print("\nChannel statistics (engineering units / volts):")
        for name in dev.channel_names():
            e = tail[name]
            v = tail[f"{name}_V"]
            print(f"  {name:8s} {np.mean(e):+10.4f} ±{np.std(e):.4f}   "
                  f"({np.mean(v):+8.4f} V ±{np.std(v) * 1000:.2f} mV)")
        print(f"\n{'PASS' if ok else 'CHECK'}: live verification "
              f"{'complete' if ok else 'found issues'}")
        return 0 if ok else 1
    finally:
        dev.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
