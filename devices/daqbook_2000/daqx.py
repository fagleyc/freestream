"""ctypes binding to the IOtech DaqX API (``DaqX64.dll``).

Only the subset needed to run a DaqBook/2000-series analog acquisition is
wrapped: open/close, scan-group setup, clock, acquisition mode, trigger,
buffered transfer, arm/disarm.  Constants are the vendor ``daqx.h`` values
(cross-checked against the IOtech Programmer's Manual).

The DLL ships with the rig's LabVIEW installation
(``...\\Shared VIs\\Devices\\IOTech\\Drivers\\Ethernet_x64\\DaqX64.dll``); the
device must additionally be given an alias (e.g. ``DaqBook2005``) in the Daq
Configuration applet (``DaqXCPL.exe`` / DaqX64.cpl) that maps it to its IP.

Nothing here opens the DLL at import time — construct :class:`DaqX` when you
actually want hardware, so the rest of the package works DLL-free (sim mode).
"""

from __future__ import annotations

import ctypes as ct
import os
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

# ─────────────────────────────────────────────────────────────────────────
#  Constants (vendor daqx.h values)
# ─────────────────────────────────────────────────────────────────────────

# Gain codes (DaqBook/2000 series PGA)
DgainX1, DgainX2, DgainX4, DgainX8 = 0, 1, 2, 3
DgainX16, DgainX32, DgainX64 = 4, 5, 6          # 2000-series only
GAIN_CODE = {1: DgainX1, 2: DgainX2, 4: DgainX4, 8: DgainX8,
             16: DgainX16, 32: DgainX32, 64: DgainX64}

# Channel flags (daqAdcSetScan)
DafUnipolar = 0x00
DafBipolar = 0x02
DafUnsigned = 0x00
DafSigned = 0x04
DafSingleEnded = 0x00
DafDifferential = 0x08
DafAnalog = 0x00

# Acquisition modes (daqAdcSetAcq)
DaamNShot = 0
DaamInfinitePost = 2
DaamPrePost = 3

# Trigger sources (daqAdcSetTrig)
DatsImmediate = 0
DatsSoftware = 1

# Transfer mask (daqAdcTransferSetBuffer)
DatmCycleOff = 0x00
DatmCycleOn = 0x01
DatmUpdateBlock = 0x00
DatmUpdateSingle = 0x02
DatmIgnoreOverruns = 0x10

# daqAdcTransferGetStat active flags
DaafAcqActive = 0x01
DaafAcqTriggered = 0x02
DaafTransferActive = 0x04

# Device types (DaqHardwareVersion) / sub types
DaqBook2000 = 23                # DaqBook/2000-series main type
DaqSubTypeDaqBook2000A = 0
DaqSubTypeDaqBook2000E = 1
DaqSubTypeDaqBook2001 = 2
DaqSubTypeDaqBook2020 = 3
DaqSubTypeDaqBook2005 = 4

# Device inventory (daqCreateDevice / daqGetDeviceInventory)
DaqInfoTypeTcp = 2
DaqIPModeAutoDetect = 0
DaqIPModeManualIP = 1
DaqInfoFlagsCreated = 0x00000003
DaqInfoFlagsNotCreated = 0x00000002
DaqInfoFlagsDetected = 0x0000000C
DaqInfoFlagsNew = DaqInfoFlagsDetected | DaqInfoFlagsNotCreated

# Full-scale span of the 2000-series front end (volts, gain ×1)
FULL_SCALE_V = 10.0
ADC_COUNTS = 65536          # 16-bit

# Default DLL search locations (rig LabVIEW install first)
DEFAULT_DLL_PATHS = [
    r"C:\Users\Casey\Nextcloud\Software\labview\Shared VIs\Devices\IOTech"
    r"\Drivers\Ethernet_x64\DaqX64.dll",
    r"C:\Program Files\IOtech\DaqX\DaqX64.dll",
    r"C:\Windows\System32\DaqX64.dll",
    "DaqX64.dll",           # PATH
]


def counts_to_volts(counts, gain: int, bipolar: bool):
    """Convert raw unsigned 16-bit ADC counts to volts.

    Works on scalars or numpy arrays.  Bipolar span is ±10/gain V mapped
    over the full unsigned range; unipolar is 0..10/gain V.
    """
    span = FULL_SCALE_V / max(gain, 1)
    if bipolar:
        return (counts / (ADC_COUNTS / 2.0) - 1.0) * span
    return counts / float(ADC_COUNTS) * span


def range_for(gain: int, bipolar: bool) -> Tuple[float, float]:
    """(lo, hi) volts of a gain/polarity combination."""
    span = FULL_SCALE_V / max(gain, 1)
    return (-span, span) if bipolar else (0.0, span)


def pick_range(v_min: float, v_max: float,
               differential: bool = True) -> Tuple[int, bool]:
    """Smallest (gain, bipolar) whose native range covers [v_min, v_max].

    Mirrors what the rig's LabVIEW TDAQ VIs do with the requested Chan
    Min/Max V (e.g. 0..3 V diff -> unipolar ×2 = 0..5 V).

    Hardware constraint (verified on the DaqBook/2005, DaqX error 134):
    **single-ended channels only support bipolar ranges**; unipolar is
    valid on differential channels only.
    """
    candidates = []
    polarities = (False, True) if differential else (True,)
    for gain in sorted(GAIN_CODE):
        for bipolar in polarities:
            lo, hi = range_for(gain, bipolar)
            if lo <= v_min and v_max <= hi:
                candidates.append((hi - lo, gain, bipolar))
    if not candidates:
        return 1, True          # widest available: ±10 V
    _, gain, bipolar = min(candidates)
    return gain, bipolar


class DaqInfoTcpT(ct.Structure):
    _fields_ = [("IPMode", ct.c_uint32),
                ("SerialNum", ct.c_char * 32),
                ("IPAddress", ct.c_char * 32)]


class _DevInfoUnion(ct.Union):
    _fields_ = [("Tcp", DaqInfoTcpT),
                ("ReservedInfo", ct.c_char * 512)]


class DaqDevInfoT(ct.Structure):
    """Vendor DaqDevInfoT (daqx.h) — device inventory / creation record."""
    _fields_ = [("AliasName", ct.c_char * 64),
                ("DeviceType", ct.c_uint32),
                ("DeviceSubType", ct.c_uint32),
                ("Reserved1", ct.c_uint32),
                ("Reserved2", ct.c_uint32),
                ("InfoType", ct.c_uint32),
                ("Info", _DevInfoUnion)]


class DaqXError(RuntimeError):
    """A DaqX call returned a non-zero error code."""

    def __init__(self, func: str, code: int):
        super().__init__(f"{func} failed with DaqX error {code} (0x{code:X})")
        self.func = func
        self.code = code


class DaqX:
    """Thin wrapper around the DaqX DLL (one instance may serve many handles)."""

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
            raise OSError(
                f"Could not load DaqX64.dll (tried {paths}): {last_exc}")

    # ── call plumbing ────────────────────────────────────────────────────
    def _call(self, name: str, *args) -> None:
        fn = getattr(self.dll, name)
        code = fn(*args)
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

    def online(self, handle: int) -> bool:
        online = ct.c_uint32(0)
        try:
            self._call("daqOnline", handle, ct.byref(online))
        except (DaqXError, AttributeError):
            return True         # not all builds export daqOnline
        return bool(online.value)

    # ── scan group / clock / acquisition ─────────────────────────────────
    def adc_set_scan(self, handle: int, channels: Sequence[int],
                     gains: Sequence[int], flags: Sequence[int]) -> None:
        n = len(channels)
        ChanArr = ct.c_uint32 * n
        self._call("daqAdcSetScan", handle,
                   ChanArr(*channels), ChanArr(*gains), ChanArr(*flags),
                   ct.c_uint32(n))

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

    # ── buffered transfer ────────────────────────────────────────────────
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
        """Return (active_flags, total_scans_transferred)."""
        active = ct.c_uint32(0)
        ret = ct.c_uint32(0)
        self._call("daqAdcTransferGetStat", handle,
                   ct.byref(active), ct.byref(ret))
        return int(active.value), int(ret.value)

    def arm(self, handle: int) -> None:
        self._call("daqAdcArm", handle)

    def disarm(self, handle: int) -> None:
        self._call("daqAdcDisarm", handle)


    # ── device inventory / alias management ──────────────────────────────
    def create_device(self, alias: str, ip: str,
                      device_type: int = DaqBook2000,
                      sub_type: int = DaqSubTypeDaqBook2005) -> None:
        """Create the per-PC device alias (what the config applet does).

        Writes the DaqX registry config so ``daqOpen(alias)`` can find the
        device at ``ip``.  May require an elevated process the first time
        (the DLL creates an HKLM key).
        """
        info = DaqDevInfoT()
        info.AliasName = alias.encode("ascii")
        info.DeviceType = device_type
        info.DeviceSubType = sub_type
        info.InfoType = DaqInfoTypeTcp
        info.Info.Tcp.IPMode = DaqIPModeManualIP
        info.Info.Tcp.IPAddress = ip.encode("ascii")
        self._call("daqCreateDevice", ct.byref(info))

    def delete_device(self, alias: str) -> None:
        self._call("daqDeleteDevice", alias.encode("ascii"))

    def device_inventory(self, flags: int = DaqInfoFlagsCreated,
                         max_devices: int = 32) -> List[dict]:
        """List devices known to DaqX (created and/or network-detected)."""
        arr = (DaqDevInfoT * max_devices)()
        count = ct.c_uint32(max_devices)
        self._call("daqGetDeviceInventory", arr, ct.byref(count),
                   None, ct.c_uint32(flags))
        out = []
        for i in range(count.value):
            d = arr[i]
            entry = {
                "alias": d.AliasName.decode("ascii", "ignore"),
                "device_type": int(d.DeviceType),
                "sub_type": int(d.DeviceSubType),
                "info_type": int(d.InfoType),
            }
            if d.InfoType == DaqInfoTypeTcp:
                entry["ip"] = d.Info.Tcp.IPAddress.decode("ascii", "ignore")
                entry["serial"] = d.Info.Tcp.SerialNum.decode("ascii", "ignore")
                entry["ip_mode"] = int(d.Info.Tcp.IPMode)
            out.append(entry)
        return out


def build_channel_flags(differential: bool, bipolar: bool) -> int:
    """Combine polarity/mode into the daqAdcSetScan flag word."""
    flags = DafAnalog | DafUnsigned
    flags |= DafDifferential if differential else DafSingleEnded
    flags |= DafBipolar if bipolar else DafUnipolar
    return flags
