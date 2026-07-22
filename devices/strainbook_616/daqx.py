"""ctypes binding to the IOtech DaqX API for the StrainBook/616.

Same DLL as the DaqBook (``DaqX64.dll``), plus the WaveBook/WBK16 channel
option interface (``daqSetOption``) that carries all strain-specific
configuration: bridge mode, excitation, filter, coupling, gain (IAG × PGA),
inversion, SSH and shunt cal.

All constants are the vendor ``daqx.h`` values (Dcot/Dcov Wbk16 enums) —
NOT the simplified codes in the older markdown reference, which do not
match the shipped DLL.

Signal architecture (StrainBook/616): each channel runs through an
instrumentation amp (IAG ×1/×10/×100/×1000) then a PGA (×1.00 … ×20.00 in
13 steps) into a ±5 V, 16-bit ADC. Requested mV ranges snap to the nearest
IAG×PGA combination (e.g. ±11 mV → ×447, ±32 mV → ×155.8 — the two ranges
in the rig's standard LabVIEW setup).
"""

from __future__ import annotations

import ctypes as ct
import os
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# ─────────────────────────────────────────────────────────────────────────
#  Shared DaqX constants (vendor daqx.h)
# ─────────────────────────────────────────────────────────────────────────

# WaveBook scan gain codes: per daqx.h, digital gains are "NOT currently
# used" for WBK channels — pass WgcX1 and set the real gain via options.
WgcX1 = 0

# Channel flags (daqAdcSetScan) — same vendor values as the DaqBook
# binding, but flags are PER CHANNEL on the StrainBook (live-verified
# 2026-07-16): bridge channels scan BIPOLAR (±5 V input-referred, signed
# data, 0 V ≈ 0 counts); the 0–10 V excitation-readback channel scans
# UNIPOLAR (0–10 V mapped one half-span down, 0 V = −FS). A bridge
# channel scanned unipolar rails at 0x8000 — that was the original
# "saturated values" bug.
DafAnalog = 0x00
DafUnipolar = 0x00
DafBipolar = 0x02
DafUnsigned = 0x00
DafSigned = 0x04
DafSingleEnded = 0x00
DafDifferential = 0x08     # StrainBook inputs are differential

# Acquisition / trigger / transfer
DaamInfinitePost = 2
DatsImmediate = 0
DatmCycleOn = 0x01
DatmUpdateSingle = 0x02
DatmIgnoreOverruns = 0x10

# daqSetOption flags
DcofChannel = 0x00
DcofModule = 0x01

# ── WBK16 channel option types (DcotWbk16*) ──
DcotWbk16Bridge = 0
DcotWbk16ShuntCal = 1
DcotWbk16InDiag = 2
DcotWbk16OffsetDac = 3
DcotWbk16OutSource = 4
DcotWbk16Inv = 5
DcotWbk16FilterType = 6
DcotWbk16Couple = 7
DcotWbk16Sample = 8        # SSH
DcotWbk16ExcDac = 9
DcotWbk16IAG = 10
DcotWbk16PGA = 11
DmotWbk16Immediate = 12    # module-level immediate commands

# ── WBK16 option values (DcovWbk16*) ──
BRIDGE_FULL = 0            # DcovWbk16ApplyFull
BRIDGE_HALF_QTR_POS = 1
BRIDGE_HALF_QTR_NEG = 2
BRIDGE_NAMES = {0: "Full", 1: "Half/Qtr+", 2: "Half/Qtr-"}

SHUNT_NONE = 0
SHUNT_120 = 1
SHUNT_350 = 2
SHUNT_1K = 3
SHUNT_AUTOZERO = 4

OUT_SIGNAL = 0             # DcovWbk16ReadSignal
OUT_EXC_VOLTS = 1          # DcovWbk16ReadExcVolts
OUT_EXC_CURRENT = 2

INVERT_NORMAL = 0
INVERT_INVERTED = 1

FILTER_BYPASS = 0
FILTER_10HZ = 1
FILTER_1KHZ = 2
FILTER_NAMES = {0: "Bypass", 1: "10 Hz", 2: "1 kHz"}

COUPLE_DC = 0
COUPLE_AC = 1

SSH_BYPASSED = 0
SSH_ON = 1

# IAG (instrumentation amp) gain codes → numeric gain
IAG_GAINS = {0: 1.0, 1: 10.0, 2: 100.0, 3: 1000.0}
# PGA gain codes → numeric gain
PGA_GAINS = {0: 1.00, 1: 1.28, 2: 1.65, 3: 2.11, 4: 2.71, 5: 3.48,
             6: 4.47, 7: 5.74, 8: 7.37, 9: 9.46, 10: 12.14, 11: 15.58,
             12: 20.00}

# Excitation DAC calibrated values
EXC_DAC = {0.0: 0x0000, 0.5: 0x1000, 1.0: 0x2000, 2.0: 0x3000,
           5.0: 0x4000, 10.0: 0x5000}
DmovWbk16ExcSrcApply = 0

ADC_FS_V = 5.0             # WaveBook-family ADC full scale (±5 V)
ADC_COUNTS = 65536

# The StrainBook/616's built-in strain conditioning is exposed by the DLL
# as an internal WBK16 bank on channels 9–16 (verified live: channels 0–8
# report module type Wbk516A and reject Wbk16 options; 9+ report
# Wbk16_SSH and accept them). Front-panel "CH n" = DaqX channel n + 8.
STRAIN_CHANNEL_OFFSET = 8

# Device inventory constants (same as daqbook binding)
DaqInfoTypeTcp = 2
DaqInfoFlagsCreated = 0x00000003

DEFAULT_DLL_PATHS = [
    r"C:\Users\Casey\Nextcloud\Software\labview\Shared VIs\Devices\IOTech"
    r"\Drivers\Ethernet_x64\DaqX64.dll",
    r"C:\Program Files\IOtech\DaqX\DaqX64.dll",
    r"C:\Windows\System32\DaqX64.dll",
    "DaqX64.dll",
]


def gain_table() -> List[Tuple[float, int, int]]:
    """All (total_gain, iag_code, pga_code) combos, ascending by gain."""
    combos = []
    for ic, ig in IAG_GAINS.items():
        for pc, pg in PGA_GAINS.items():
            combos.append((ig * pg, ic, pc))
    return sorted(combos)


def pick_gain_for_range(mv_span: float) -> Tuple[float, int, int]:
    """Largest gain whose input-referred full scale covers ±mv_span.

    Returns (total_gain, iag_code, pga_code).  E.g. 11 mV → ×447,
    32 mV → ×155.8, 5000 mV → ×1.
    """
    best = (1.0, 0, 0)
    for total, ic, pc in gain_table():
        fs_mv = ADC_FS_V / total * 1000.0
        if fs_mv >= mv_span:
            best = (total, ic, pc)      # keep climbing while range still fits
        else:
            break
    return best


def range_mv(total_gain: float) -> float:
    """Input-referred full scale (±mV) at a total gain."""
    return ADC_FS_V / max(total_gain, 1e-9) * 1000.0


def counts_to_volts(counts, total_gain: float):
    """Raw 16-bit ADC counts → input-referred volts (±5 V FS).

    The StrainBook returns SIGNED two's-complement data (live-verified
    2026-07-16 against known physical inputs: ~100 µV bridges read
    0xFD22 ≈ −734 counts = −250 µV at ×447; an offset-binary decode of
    the same counts lands on the ±11 mV rail — that was the "all
    channels railed" bug). 0x8000 = −FS, 0 = 0 V, 0x7FFF = +FS.

    Channels scanned UNIPOLAR (the 0–10 V excitation readback) come out
    shifted one half-span down (0 V input = −FS): add ``ADC_FS_V`` to
    the decoded value to recover the true 0–10 V reading — the driver
    does this for ``read_excitation`` channels.
    """
    import numpy as np
    c = np.asarray(counts, dtype=np.float64)
    c = np.where(c >= 32768.0, c - 65536.0, c)
    out = c / 32768.0 * (ADC_FS_V / total_gain)
    return float(out) if out.ndim == 0 else out


class DaqXError(RuntimeError):
    def __init__(self, func: str, code: int):
        super().__init__(f"{func} failed with DaqX error {code} (0x{code:X})")
        self.func = func
        self.code = code


class DaqInfoTcpT(ct.Structure):
    _fields_ = [("IPMode", ct.c_uint32),
                ("SerialNum", ct.c_char * 32),
                ("IPAddress", ct.c_char * 32)]


class _DevInfoUnion(ct.Union):
    _fields_ = [("Tcp", DaqInfoTcpT), ("ReservedInfo", ct.c_char * 512)]


class DaqDevInfoT(ct.Structure):
    _fields_ = [("AliasName", ct.c_char * 64),
                ("DeviceType", ct.c_uint32),
                ("DeviceSubType", ct.c_uint32),
                ("Reserved1", ct.c_uint32),
                ("Reserved2", ct.c_uint32),
                ("InfoType", ct.c_uint32),
                ("Info", _DevInfoUnion)]


class DaqX:
    """Thin DaqX wrapper (StrainBook flavour: adds daqSetOption)."""

    def __init__(self, dll_path: Optional[str] = None):
        paths = [dll_path] if dll_path else DEFAULT_DLL_PATHS
        last_exc: Optional[Exception] = None
        self.dll = None
        for p in paths:
            if p is None:
                continue
            try:
                if os.path.sep in str(p) and not Path(p).exists():
                    continue
                self.dll = ct.WinDLL(str(p))
                self.dll_path = str(p)
                break
            except OSError as exc:
                last_exc = exc
        if self.dll is None:
            raise OSError(f"Could not load DaqX64.dll (tried {paths}): "
                          f"{last_exc}")
        # daqSetOption takes a FLOAT value — declare it so x64 passes it
        # in the right register.
        self.dll.daqSetOption.argtypes = [
            ct.c_int64 if ct.sizeof(ct.c_void_p) == 8 else ct.c_int32,
            ct.c_uint32, ct.c_uint32, ct.c_uint32, ct.c_float]
        self.dll.daqSetOption.restype = ct.c_int32

        # Suppress the DLL's default error handler — it pops a BLOCKING
        # modal ("FIFO Full … continue? Yes/No") that freezes the app.
        # Register a no-op handler so errors surface only as return codes
        # (our callers already raise DaqXError on nonzero and handle it).
        try:
            self._ERRH = ct.CFUNCTYPE(ct.c_uint32, ct.c_uint32)
            self._err_handler = self._ERRH(lambda code: 0)
            self.dll.daqSetDefaultErrorHandler.argtypes = [self._ERRH]
            self.dll.daqSetDefaultErrorHandler.restype = ct.c_uint32
            self.dll.daqSetDefaultErrorHandler(self._err_handler)
        except Exception:                              # noqa: BLE001
            pass          # older DLL without the entry point — harmless

    def _call(self, name: str, *args) -> None:
        code = getattr(self.dll, name)(*args)
        if code != 0:
            raise DaqXError(name, code)

    # ── session ──────────────────────────────────────────────────────────
    def open(self, device_name: str) -> int:
        handle = self.dll.daqOpen(device_name.encode("ascii"))
        if handle < 0:
            raise DaqXError("daqOpen", handle)
        return int(handle)

    def close(self, handle: int) -> None:
        self._call("daqClose", handle)

    # ── channel options ──────────────────────────────────────────────────
    def set_option(self, handle: int, chan: int, option_type: int,
                   value: float, flags: int = DcofChannel) -> None:
        self._call("daqSetOption", handle, ct.c_uint32(chan),
                   ct.c_uint32(flags), ct.c_uint32(option_type),
                   ct.c_float(value))

    # ── scan / clock / acquisition (as daqbook binding) ─────────────────
    def adc_set_scan(self, handle: int, channels: Sequence[int],
                     gains: Sequence[int], flags: Sequence[int]) -> None:
        n = len(channels)
        Arr = ct.c_uint32 * n
        self._call("daqAdcSetScan", handle, Arr(*channels), Arr(*gains),
                   Arr(*flags), ct.c_uint32(n))

    def adc_set_freq(self, handle: int, hz: float) -> None:
        self._call("daqAdcSetFreq", handle, ct.c_float(hz))

    def adc_get_freq(self, handle: int) -> float:
        f = ct.c_float(0.0)
        self._call("daqAdcGetFreq", handle, ct.byref(f))
        return float(f.value)

    def adc_set_acq(self, handle: int, mode: int,
                    pre: int = 0, post: int = 0) -> None:
        self._call("daqAdcSetAcq", handle, ct.c_uint32(mode),
                   ct.c_uint32(pre), ct.c_uint32(post))

    def adc_set_trig(self, handle: int, source: int = DatsImmediate,
                     rising: bool = True, level: int = 0,
                     hysteresis: int = 0, channel: int = 0) -> None:
        self._call("daqAdcSetTrig", handle, ct.c_uint32(source),
                   ct.c_uint32(1 if rising else 0), ct.c_uint16(level),
                   ct.c_uint16(hysteresis), ct.c_uint32(channel))

    def make_buffer(self, scan_count: int, n_channels: int):
        return (ct.c_uint16 * (scan_count * n_channels))()

    def transfer_set_buffer(self, handle: int, buf, scan_count: int,
                            mask: int = (DatmCycleOn | DatmUpdateSingle |
                                         DatmIgnoreOverruns)) -> None:
        self._call("daqAdcTransferSetBuffer", handle, buf,
                   ct.c_uint32(scan_count), ct.c_uint32(mask))

    def transfer_start(self, handle: int) -> None:
        self._call("daqAdcTransferStart", handle)

    def transfer_stop(self, handle: int) -> None:
        self._call("daqAdcTransferStop", handle)

    def transfer_get_stat(self, handle: int) -> Tuple[int, int]:
        active = ct.c_uint32(0)
        ret = ct.c_uint32(0)
        self._call("daqAdcTransferGetStat", handle,
                   ct.byref(active), ct.byref(ret))
        return int(active.value), int(ret.value)

    def arm(self, handle: int) -> None:
        self._call("daqAdcArm", handle)

    def disarm(self, handle: int) -> None:
        self._call("daqAdcDisarm", handle)

    # ── inventory (diagnostics) ──────────────────────────────────────────
    def device_inventory(self, flags: int = DaqInfoFlagsCreated,
                         max_devices: int = 32) -> List[Dict]:
        arr = (DaqDevInfoT * max_devices)()
        count = ct.c_uint32(max_devices)
        self._call("daqGetDeviceInventory", arr, ct.byref(count),
                   None, ct.c_uint32(flags))
        out = []
        for i in range(count.value):
            d = arr[i]
            entry = {"alias": d.AliasName.decode("ascii", "ignore"),
                     "device_type": int(d.DeviceType),
                     "sub_type": int(d.DeviceSubType)}
            if d.InfoType == DaqInfoTypeTcp:
                entry["ip"] = d.Info.Tcp.IPAddress.decode("ascii", "ignore")
                entry["serial"] = d.Info.Tcp.SerialNum.decode("ascii",
                                                              "ignore")
            out.append(entry)
        return out
