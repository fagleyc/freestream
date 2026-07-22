"""Headless sim smoke test: calibrated move, wrong-way trip, stall abort.

    python -m traverse_swt._smoke_sim
"""

from __future__ import annotations

import sys
import time

if hasattr(sys.stdout, "reconfigure"):      # Windows cp1252 console vs "−"
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from traverse_swt.config import TraverseConfig
from traverse_swt.device import TraverseDrive


def main() -> int:
    cfg = TraverseConfig(force_sim=True, loop_ms=20)
    # rig-like signed slopes, re-zeroed at counts 0 = 0"
    for ax, slope in ((cfg.x, 13705.6), (cfg.y, -14841.0)):
        ax.clicks_per_inch = slope
        ax.inch_high, ax.counts_high, ax.calibrated = 0.0, 0, True
        ax.min_in, ax.max_in = -6.0, 6.0

    drive = TraverseDrive(cfg)
    msgs = []
    drive.on_status = lambda m: msgs.append(m) or print("  |", m)

    print("connect …")
    drive.connect()
    assert drive.connected and drive.sim_mode
    drive._plc.sim_rate = 25_000     # fast sim plant (rig-realistic: 2000)

    print("calibrated move on X …")
    done = []
    drive.on_move_complete = done.append
    drive.move_to(x=2.0)
    t0 = time.time()
    while time.time() - t0 < 10 and "X" not in done:
        time.sleep(0.05)
    st = drive.state()["X"]
    assert "X" in done, f"move never completed: {st}"
    err = abs(st["inches"] - 2.0)
    assert err <= cfg.x.tolerance_in * 3, f"stopped {err:.4f}\" off"
    print(f"  X at {st['inches']:+.4f}\" (target +2.0000\", counts "
          f"{st['counts']:+d})")

    print("calibrated move on Y (negative slope) …")
    done.clear()
    drive.move_to(y=0.5)
    t0 = time.time()
    while time.time() - t0 < 10 and "Y" not in done:
        time.sleep(0.05)
    st = drive.state()["Y"]
    assert "Y" in done, f"Y move never completed: {st}"
    err = abs(st["inches"] - 0.5)
    assert err <= cfg.y.tolerance_in * 3, f"stopped {err:.4f}\" off"
    print(f"  Y at {st['inches']:+.4f}\" (target +0.5000\", counts "
          f"{st['counts']:+d})")

    print("wrong-way trip: flip Y's direction sense and command a move …")
    cfg.y.fwd_increases_counts = False   # now the driver drives backwards
    drive.move_to(y=0.0)
    t0 = time.time()
    tripped = False
    while time.time() - t0 < 5:
        if any("WRONG WAY" in m for m in msgs):
            tripped = True
            break
        time.sleep(0.05)
    st = drive.state()["Y"]
    assert tripped, "wrong-way trip never fired"
    assert not st["moving"], "axis still moving after wrong-way trip"
    cfg.y.fwd_increases_counts = True

    print("stall abort: freeze a commanded module and watch it abort …")
    cfg.stall_abort_ticks = 30           # ~0.6 s at the 20 ms loop
    msgs.clear()
    drive._plc.stalled_axes.add("X")     # faulted stepper module
    drive.move_to(x=0.0)
    t0 = time.time()
    aborted = False
    while time.time() - t0 < 5:
        if any("ABORTED: X" in m for m in msgs):
            aborted = True
            break
        time.sleep(0.05)
    assert aborted, "stalled move never aborted"
    assert not drive.state()["X"]["moving"], "still commanded after abort"
    drive._plc.stalled_axes.discard("X")

    drive.stop_all()
    drive.disconnect()
    assert not drive.connected
    print("\nSMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
