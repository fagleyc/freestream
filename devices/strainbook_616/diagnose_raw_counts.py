#!/usr/bin/env python
"""Raw-counts diagnostic for the StrainBook/616 (live hardware).

Configures the device exactly like the driver (bridge channels bipolar,
excitation readback unipolar, all reading their INPUT signal), acquires a
short burst, and prints per-channel raw 16-bit counts alongside the
decoded volts. Use this to separate decode problems from configuration
problems: the raw column is what the hardware actually returned.

Live-verified reference (2026-07-16, external 10 V excitation, bridges
~100 uV): N1 0xFD22 -> -250 uV ... Excitation ~31868 -> +9.86 V.

    python -m strainbook_616.diagnose_raw_counts
"""

from __future__ import annotations

import sys
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np

from . import daqx
from .config import StrainbookConfig

ACQ_S = 0.8
HZ = 200.0


def main() -> int:
    cfg = StrainbookConfig()
    chans = cfg.enabled_channels()
    lib = daqx.DaqX(cfg.dll_path or None)
    h = lib.open(cfg.device_name)
    try:
        for ch in chans:
            c = ch.channel + daqx.STRAIN_CHANNEL_OFFSET
            _t, iag, pga = ch.gain
            lib.set_option(h, c, daqx.DcotWbk16OutSource, daqx.OUT_SIGNAL)
            lib.set_option(h, c, daqx.DcotWbk16Bridge, ch.bridge)
            lib.set_option(h, c, daqx.DcotWbk16IAG, iag)
            lib.set_option(h, c, daqx.DcotWbk16PGA, pga)
            lib.set_option(h, c, daqx.DcotWbk16FilterType, ch.filter_type)
            lib.set_option(h, c, daqx.DcotWbk16Couple,
                           daqx.COUPLE_AC if ch.ac_couple
                           else daqx.COUPLE_DC)
            lib.set_option(h, c, daqx.DcotWbk16Inv, daqx.INVERT_NORMAL)
            lib.set_option(h, c, daqx.DcotWbk16Sample, daqx.SSH_BYPASSED)
            lib.set_option(h, c, daqx.DcotWbk16ShuntCal, daqx.SHUNT_NONE)

        channels = [c.channel + daqx.STRAIN_CHANNEL_OFFSET for c in chans]
        base = daqx.DafAnalog | daqx.DafUnsigned | daqx.DafDifferential
        flags = [base | (daqx.DafUnipolar if c.read_excitation
                         else daqx.DafBipolar) for c in chans]
        lib.adc_set_scan(h, channels, [daqx.WgcX1] * len(channels), flags)
        lib.adc_set_freq(h, HZ)
        lib.adc_set_acq(h, daqx.DaamInfinitePost)
        lib.adc_set_trig(h, daqx.DatsImmediate)
        n_scans = int(ACQ_S * HZ) + 50
        buf = lib.make_buffer(n_scans, len(chans))
        lib.transfer_set_buffer(h, buf, n_scans)
        lib.transfer_start(h)
        lib.arm(h)
        deadline = time.perf_counter() + ACQ_S + 2.0
        total = 0
        while time.perf_counter() < deadline:
            _a, total = lib.transfer_get_stat(h)
            if total >= int(ACQ_S * HZ):
                break
            time.sleep(0.05)
        for op in (lib.disarm, lib.transfer_stop):
            try:
                op(h)
            except daqx.DaqXError:
                pass
        n = min(total, n_scans)
        raw = (np.ctypeslib.as_array(buf).reshape(n_scans, len(chans))
               [:n, :].astype(np.int64))
        print(f"{n} scans @ {HZ:.0f} Hz")
        print(f"{'ch':<12}{'rawmin':>7} {'rawmean':>8} {'rawmax':>7}"
              f"   {'hex':>6}  {'volts':>14}")
        for i, ch in enumerate(chans):
            col = raw[:, i]
            v = float(np.mean(daqx.counts_to_volts(col, ch.gain[0])))
            if ch.read_excitation:
                v += daqx.ADC_FS_V
            m = int(np.mean(col))
            print(f"{ch.name:<12}{col.min():>7d} {m:>8d} {col.max():>7d}"
                  f"   0x{m:04X}  {v:>+12.6f} V")
        return 0
    finally:
        try:
            lib.close(h)
        except daqx.DaqXError:
            pass


if __name__ == "__main__":
    raise SystemExit(main())
