#!/usr/bin/env python
"""Read-only probe of the real ATE balance OGI (default 192.168.1.60).

Safe by construction: listens for the OGI to dial in, receives the LOADS
stream, and issues only status queries (GET_POSITIONS / GET_FILTERS /
GET_LOCK_STATUS). It never zeros, locks, or commands motion.

    python probe_ate_rig.py [--ip 192.168.1.60] [--seconds 15]

Reports: link status, LOADS rate, packet variant (29 vs 33 byte / sync),
live load values, and the replies to the three status queries.
"""

import argparse
import logging
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ate_balance import protocol as P
from ate_balance.config import AteConfig
from ate_balance.device import AteBalanceDevice


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only ATE rig probe")
    ap.add_argument("--ip", default="192.168.1.60")
    ap.add_argument("--seconds", type=float, default=15.0,
                    help="how long to collect the LOADS stream")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    cfg = AteConfig(ogi_ip=args.ip)
    dev = AteBalanceDevice(cfg)
    frames = []
    replies = []
    dev.on_frame = frames.append
    dev.on_reply = replies.append

    print(f"Probing OGI at {args.ip} — listening on TCP {cfg.tmsc_port} / "
          f"UDP {cfg.tmsd_port}, trigger to UDP {cfg.ogit_port} ...")
    try:
        dev.connect()          # binds + sends TMS_CONNECT (auto_trigger)
    except OSError as exc:
        print(f"FAIL: could not bind local ports: {exc}")
        print("Another TMS client (or a stale python) may hold 3040/3041.")
        return 2
    dev.start()

    try:
        # Wait for the control link
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < 10.0 and not dev.link_up:
            time.sleep(0.1)
        if not dev.link_up:
            print("NO LINK: OGI did not dial back within 10 s.")
            print("  - Is the OGI software running on the rig PC?")
            print("  - Windows Firewall may be blocking inbound TCP 3040 / "
                  "UDP 3041 for python.exe.")
            if frames:
                print(f"  (But {len(frames)} LOADS datagrams DID arrive — "
                      "control channel only is blocked.)")
        else:
            print("LINKED: OGI control connection established.")

        # Status queries (read-only)
        sent = {}
        for name, fn in (("GET_POSITIONS", dev.get_positions),
                         ("GET_FILTERS", dev.get_filters),
                         ("GET_LOCK_STATUS", dev.get_lock_status)):
            if dev.link_up:
                sent[fn()] = name
                time.sleep(0.3)

        # Collect stream
        print(f"Collecting LOADS for {args.seconds:.0f} s ...")
        t0 = time.perf_counter()
        n0 = len(frames)
        time.sleep(args.seconds)
        n1 = len(frames)
        dt = time.perf_counter() - t0
        rate = (n1 - n0) / dt if dt > 0 else 0.0

        print("\n---- RESULTS ----")
        print(f"Control link : {'UP' if dev.link_up else 'DOWN'}")
        print(f"LOADS frames : {n1} total, {rate:.1f} Hz")
        if frames:
            variant = ("33-byte (with int32 sync)" if dev.last_had_sync
                       else "29-byte (no sync word — USAFA build)")
            print(f"Packet type  : {variant}")
            last = frames[-1].loads
            print("Latest loads (N / N·m):")
            for ax in P.WIRE_AXES:
                vals = [f.loads[ax] for f in frames[-min(200, len(frames)):]]
                print(f"  {ax:6s} {last[ax]:+10.3f}   "
                      f"(mean {statistics.fmean(vals):+10.3f}, "
                      f"sd {statistics.pstdev(vals):.3f} "
                      f"over last {len(vals)})")
        else:
            print("Packet type  : n/a - no LOADS datagrams received")
            print("  - OGI may stream only after it is in a running state,")
            print("  - or Windows Firewall is blocking inbound UDP 3041.")
        if replies:
            print("Replies:")
            for r in replies:
                tag = sent.get(r.serial, "?")
                print(f"  [{tag}] {r.command} {' '.join(r.params)}")
        elif dev.link_up:
            print("Replies      : none received (queries unanswered)")
        return 0
    finally:
        dev.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
