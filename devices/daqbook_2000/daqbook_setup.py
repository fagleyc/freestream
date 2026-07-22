#!/usr/bin/env python
"""DaqX device-alias setup/diagnostic tool (replaces the config applet).

    python daqbook_setup.py list                 # created + network-detected
    python daqbook_setup.py create [alias] [ip]  # default DaqBook2005 192.168.1.125
    python daqbook_setup.py delete <alias>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from daqbook_2000 import daqx


def show(title, devices):
    print(f"{title}:")
    if not devices:
        print("   (none)")
    for d in devices:
        line = (f"   alias='{d['alias']}'  type={d['device_type']}"
                f"/{d['sub_type']}")
        if "ip" in d:
            mode = "auto" if d.get("ip_mode") == 0 else "manual"
            line += f"  ip={d['ip']} ({mode})  serial='{d['serial']}'"
        print(line)


def main() -> int:
    args = sys.argv[1:] or ["list"]
    lib = daqx.DaqX()
    print(f"DLL: {lib.dll_path}")

    if args[0] == "list":
        try:
            show("Created (registry) devices",
                 lib.device_inventory(daqx.DaqInfoFlagsCreated))
        except daqx.DaqXError as exc:
            print(f"   created-inventory failed: {exc}")
        try:
            show("Network-detected devices",
                 lib.device_inventory(daqx.DaqInfoFlagsDetected))
        except daqx.DaqXError as exc:
            print(f"   detect-inventory failed: {exc}")
        return 0

    if args[0] == "create":
        alias = args[1] if len(args) > 1 else "DaqBook2005"
        ip = args[2] if len(args) > 2 else "192.168.1.125"
        try:
            lib.create_device(alias, ip)
        except daqx.DaqXError as exc:
            print(f"daqCreateDevice failed: {exc}")
            print("If this is a registry-permission error, re-run once from "
                  "an elevated (Administrator) prompt.")
            return 1
        print(f"Created alias '{alias}' -> {ip}")
        try:
            h = lib.open(alias)
            lib.close(h)
            print("daqOpen check: OK")
        except daqx.DaqXError as exc:
            print(f"daqOpen check failed: {exc}")
            return 1
        return 0

    if args[0] == "delete" and len(args) > 1:
        lib.delete_device(args[1])
        print(f"Deleted alias '{args[1]}'")
        return 0

    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
