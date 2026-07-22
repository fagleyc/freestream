#!/usr/bin/env python
"""Read-only probe of the DaqBook/2005 path: DLL load + daqOpen + one block.

    python probe_daqbook.py [--device DaqBook2005] [--dll <path>]

Safe: analog input only, nothing is written to the device.
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daqbook_2000 import daqx
from daqbook_2000.config import DaqbookConfig
from daqbook_2000.device import Daqbook2000


def main() -> int:
    ap = argparse.ArgumentParser(description="DaqBook read-only probe")
    ap.add_argument("--device", default="DaqBook2005")
    ap.add_argument("--dll", default="")
    args = ap.parse_args()

    print("1) Loading DaqX64.dll ...")
    try:
        lib = daqx.DaqX(args.dll or None)
    except OSError as exc:
        print(f"   FAIL: {exc}")
        return 2
    print(f"   OK: {lib.dll_path}")

    print(f"2) daqOpen('{args.device}') ...")
    try:
        handle = lib.open(args.device)
    except daqx.DaqXError as exc:
        print(f"   FAIL: {exc}")
        print("   The device alias is not configured on this PC.")
        print("   Fix: run the Daq Configuration applet (DaqX64.cpl, or")
        print("   DaqXCPL.exe in the IOTech\\DaqView folder), add a")
        print(f"   DaqBook/2000-series Ethernet device named '{args.device}'")
        print("   at IP 192.168.1.125, then re-run this probe.")
        return 3
    print(f"   OK: handle {handle}")

    print("3) One second of acquisition via the full driver ...")
    lib.close(handle)
    cfg = DaqbookConfig(device_name=args.device,
                        dll_path=args.dll)
    dev = Daqbook2000(cfg)
    dev.on_status = lambda s: print(f"   [status] {s}")
    try:
        dev.connect()
        dev.start()
        time.sleep(1.5)
        latest = dev.latest()
        print(f"   scans: {dev.frame_count()}  actual rate: "
              f"{dev.actual_hz:.1f} Hz")
        if latest:
            for name in dev.channel_names():
                print(f"   {name:8s} {latest[name]:+10.4f} "
                      f"({latest[name + '_V']:+8.4f} V)")
        return 0
    finally:
        dev.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
