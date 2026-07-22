"""Live readout from a Heise PM indicator (read-only).

Usage:
  python probe_heise.py COM5                 # live pressure+temperature
  python probe_heise.py COM5 --unit kPa      # select pressure unit
  python probe_heise.py --sim                # emulator, no hardware
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from heise import HeiseConfig, HeiseGauge, PRESSURE_UNITS  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Live Heise indicator readout")
    parser.add_argument("com_port", nargs="?", default="",
                        help="COM port (e.g. COM5)")
    parser.add_argument("--sim", action="store_true")
    parser.add_argument("--baud", type=int, default=9600)
    parser.add_argument("--unit", default="",
                        help=f"pressure unit: "
                             f"{'/'.join(PRESSURE_UNITS.values())}")
    parser.add_argument("--seconds", type=float, default=10.0)
    args = parser.parse_args(argv)

    cfg = HeiseConfig(com_port=args.com_port, baud=args.baud,
                      force_sim=args.sim)
    if args.unit:
        cfg.left.unit = args.unit
    dev = HeiseGauge(cfg)
    dev.on_status = lambda m: print("  status:", m)
    dev.connect()
    try:
        t_end = time.time() + args.seconds
        while time.time() < t_end:
            time.sleep(max(cfg.poll_s, 0.25))
            latest = dev.latest()
            if latest:
                vals = "  ".join(
                    f"{p.name}: {latest.get(p.name, float('nan')):10.4f}"
                    f" {p.unit}" for p in cfg.enabled_ports())
                print(f"  {vals}")
        print(f"battery: {dev.battery():.2f} V")
    finally:
        dev.disconnect()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
