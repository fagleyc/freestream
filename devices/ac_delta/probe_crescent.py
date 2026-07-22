#!/usr/bin/env python
"""Read-only probe of the ARC Crescent drives — NO motion commands.

Connects Modbus to both axes, reads encoders for a few seconds, reports
raw counts (and angles once calibrated). First real moves should be run
from the GUI with someone watching the crescent.

    python probe_crescent.py [--seconds 5]
"""

import argparse
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ac_delta.axis import AxisError, CrescentAxis
from ac_delta.config import CrescentConfig


def main() -> int:
    ap = argparse.ArgumentParser(description="Crescent read-only probe")
    ap.add_argument("--seconds", type=float, default=5.0)
    args = ap.parse_args()

    cfg = CrescentConfig()
    ok = True
    for ax_cfg in cfg.axes():
        print(f"\n{ax_cfg.name} @ {ax_cfg.ip}:{ax_cfg.port}")
        axis = CrescentAxis(ax_cfg, timeout_s=2.0)
        try:
            axis.connect()
            print("   Modbus connected")
        except AxisError as exc:
            print(f"   FAIL: {exc}")
            ok = False
            continue
        try:
            readings = []
            t0 = time.perf_counter()
            while time.perf_counter() - t0 < args.seconds:
                readings.append(axis.read_encoder())
                time.sleep(0.05)
            rate = len(readings) / args.seconds
            print(f"   {len(readings)} encoder reads ({rate:.0f}/s)")
            print(f"   encoder: last {readings[-1]:+d}  "
                  f"min {min(readings):+d}  max {max(readings):+d}  "
                  f"sd {statistics.pstdev(readings):.2f}")
            if ax_cfg.calibrated:
                print(f"   angle: {ax_cfg.encoder_to_angle(readings[-1]):+.3f} deg")
            else:
                print("   (uncalibrated — enter constants in the GUI "
                      "Calibration tab for angles)")
        except AxisError as exc:
            print(f"   FAIL during reads: {exc}")
            ok = False
        finally:
            axis.close()
    print(f"\n{'PASS' if ok else 'FAIL'}: read-only probe complete")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
