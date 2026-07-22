#!/usr/bin/env python
"""Read-only probe of the StrainBook/616 (alias StrainBook_0, 192.168.1.123).

Safe: configures channels and reads bridge voltages; excitation is set to
the configured bank voltages (10 V default, as in the rig's LabVIEW setup).

    python probe_strainbook.py [--device StrainBook_0] [--seconds 2]
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from strainbook_616 import daqx
from strainbook_616.config import StrainbookConfig
from strainbook_616.device import Strainbook616


def main() -> int:
    ap = argparse.ArgumentParser(description="StrainBook read-only probe")
    ap.add_argument("--device", default="StrainBook_0")
    ap.add_argument("--seconds", type=float, default=2.0)
    args = ap.parse_args()

    print("1) Loading DaqX64.dll ...")
    try:
        lib = daqx.DaqX()
    except OSError as exc:
        print(f"   FAIL: {exc}")
        return 2
    print(f"   OK: {lib.dll_path}")

    print(f"2) daqOpen('{args.device}') ...")
    try:
        handle = lib.open(args.device)
    except daqx.DaqXError as exc:
        print(f"   FAIL: {exc}")
        print("   Checklist: wavebk driver running? (sc query wavebk)")
        print("   Alias exists? (python daqbook_setup.py list)")
        print("   Device session free? (check http://192.168.1.123/)")
        return 3
    print(f"   OK: handle {handle}")
    lib.close(handle)

    print(f"3) {args.seconds:.0f} s of bridge acquisition via the driver ...")
    cfg = StrainbookConfig(device_name=args.device)
    dev = Strainbook616(cfg)
    dev.on_status = lambda s: print(f"   [status] {s}")
    try:
        dev.connect()
        dev.start()
        time.sleep(args.seconds + 0.5)
        latest = dev.latest()
        print(f"   scans: {dev.frame_count()}  actual rate: "
              f"{dev.actual_hz:.1f} Hz")
        if latest:
            for name in dev.channel_names():
                print(f"   {name:11s} {latest[name]:+11.4f} "
                      f"({latest[name + '_V'] * 1000:+9.4f} mV)")
        return 0
    finally:
        dev.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
