# StrainBook/616 — Python Driver Reference & Implementation

## 1. Device Overview

The StrainBook/616 is an Ethernet-based, 8-channel strain gage measurement system manufactured by IOtech (now Measurement Computing). It communicates over 10/100BaseT Ethernet and uses the DaqX API for programmatic control.

**Key Specifications**

| Parameter | Value |
|---|---|
| Built-in Channels | 8 (expandable to 64 via WBK16 modules) |
| A/D Converter | 16-bit, 1 MHz scanning |
| Max Sample Rate (8 ch) | 125 kHz/channel |
| Max Sample Rate (8 ch, SSH) | 111 kHz/channel |
| Input Type | Differential, 100 MΩ impedance |
| Coupling | AC and DC, software selectable |
| Gain Range | 1 to 20,000 in 50+ steps (28% increments) |
| CMRR | 100 dB at gains > 100 |
| Cross-Talk Rejection | > 90 dB below 1 kHz |
| Bridge Configurations | Full (4/6 wire), Half, Quarter (2/3 wire) |
| Bridge Resistance | 60 to 1,000 Ω |
| Excitation Source | Dual bank: 0.5, 1.0, 2.0, 5.0, 10.0 VDC or Off |
| Excitation Accuracy | ±5 mV |
| Excitation Current Limit | 85 mA/channel (fold-back limiting) |
| Filters | 4-pole Butterworth, 10 Hz / 1 kHz / bypass |
| Offset Adjustment | ±3V RTI at gain 1–10, scaling down with gain |
| Shunt Calibration | 3 software-selectable user-supplied resistors/channel |
| Sequencer Depth | 128 locations |
| Digital I/O | 16-bit TTL via DB25 connector |
| Communication | 10/100BaseT Ethernet |
| Sync Ports | 2 (up to 4 units synchronized) |
| Power | 10–30 VDC input |

---

## 2. Signal Path Architecture

Each channel follows this signal chain:

```
Strain Gage → DB9 Connector → Bridge Completion Network → Shunt Cal MUX
    → Instrumentation Amp (PGIA: ×1/×10/×100/×1000)
    → Offset DAC (±3V)
    → Programmable Gain Amp (PGA: ×1 to ×20 in 28% steps)
    → Filter MUX (10 Hz LPF / 1 kHz LPF / Bypass)
    → AC Coupling Option (1 Hz HPF) / Polarity Invert
    → Channel Selection MUX
    → SSH (optional, 100 ns aperture)
    → 16-bit A/D (1 µs/channel)
    → DSP (gain/offset correction from EEPROM cal)
    → FIFO Buffer → Ethernet → PC
```

**Amplifier Gain Stages**

The total gain is PGIA × PGA, giving 52 discrete steps from 1 to 20,000:

```
PGIA gains:  ×1, ×10, ×100, ×1000
PGA gains:   ×1.00, ×1.28, ×1.65, ×2.11, ×2.71, ×3.48, ×4.47,
             ×5.47, ×7.37, ×9.46, ×12.14, ×15.58, ×20.00
```

**Excitation Banks**

Bank 1 supplies channels 1–4, Bank 2 supplies channels 5–8. Each bank is independently programmable. Remote sense inputs should be connected at the gage for best accuracy; if unused, they must be jumpered to the excitation output at the DB9 connector.

---

## 3. LabVIEW VI Architecture

The LabVIEW driver follows a sequential acquisition pattern using IOtech/MCC's LabVIEW driver VIs:

### StrainBook_Example_.vi — Top-Level Flow

```
1. Locate Device By Name.vi          → Find the StrainBook on the network
2. Acquisition Initialize.vi         → Open device handle, reset state
3. StrainBook616 Input Channel       → Configure all 8 channels:
   Configuration.vi                     bridge type, gain, excitation,
                                        coupling, filter, cal params
4. Basic Trigger Configuration.vi    → Set trigger source/mode
5. Acquisition Scan Configuration.vi → Set scan list, sample rate,
                                        number of scans
6. Acquisition Arm.vi                → Arm the acquisition
7. ┌─ LOOP ──────────────────────┐
   │  Acquisition Read Scan.vi   │   → Read buffered scan data
   │  Chart Legend.vi             │   → Display waveforms
   └─────────────────────────────┘
8. Acquisition Close.vi              → Release device handle
```

### StrainBook616_Input_Channel_Configuration.vi

This VI wraps two lower-level configuration VIs:

- **WaveBook516 Analog Input Config.vi** — Sets the base analog input parameters (gain, range, coupling) that are common to the WaveBook/StrainBook platform.
- **WBK16 Channel Config.vi** — Sets strain-specific parameters (bridge type, excitation voltage, shunt cal selection, filter mode) for StrainBook and WBK16 channels.

---

## 4. DaqX API Function Map

The LabVIEW VIs wrap the C-language DaqX API. The core functions for StrainBook operation are:

| LabVIEW VI | DaqX API Function | Purpose |
|---|---|---|
| Locate Device By Name | `daqOpen(deviceName)` | Open connection to named device |
| Acquisition Initialize | `daqInit(handle)` | Initialize/reset device state |
| SB616 Input Channel Config | `daqAdcSetScan(handle, ...)` | Configure scan channels |
| | `daqSetOption(handle, chan, optType, optValue)` | Set per-channel options (bridge, excitation, filter, etc.) |
| Basic Trigger Config | `daqSetTriggerEvent(handle, ...)` | Configure trigger source and condition |
| Acq Scan Configuration | `daqAdcSetAcq(handle, mode, preTrig, postTrig)` | Set acquisition mode and scan count |
| | `daqAdcSetFreq(handle, freq)` | Set scan frequency |
| Acquisition Arm | `daqAdcArm(handle)` | Arm the acquisition |
| Acquisition Read Scan | `daqAdcTransferGetData(handle, buf, ...)` | Transfer data from FIFO to PC buffer |
| Acquisition Close | `daqClose(handle)` | Close device handle |

---

## 5. Calibration Methods

The StrainBook supports five calibration methods. All methods ultimately produce an **mX + b** linear conversion: `Engineering_Units = m × Sensor_Voltage + b`.

### 5.1 Two-Point Manual

The gold standard for accuracy. The operator applies two known physical loads and the system measures the corresponding voltages.

**Required Inputs:** Excitation voltage, max load, quiescent load, Point 1 load (units), Point 2 load (units)

**Procedure:**
1. Apply Point 1 load → system reads voltage V₁
2. Apply Point 2 load → system reads voltage V₂
3. Compute: `m = (P2_units − P1_units) / (V₂ − V₁)` and `b = P1_units − m × V₁`
4. System auto-selects optimal PGIA and PGA gain settings

### 5.2 Two-Point Automatic

No physical loads required — the user provides known voltage/load pairs from the sensor datasheet.

**Required Inputs:** Excitation, max load, quiescent load, Point 1 (mV + units), Point 2 (mV + units)

### 5.3 Shunt (Internal)

Uses one of three user-installed shunt resistors to create a known bridge imbalance. The expected microstrain from the shunt is calculated:

```
ε_shunt = -R_bridge / (R_shunt + R_bridge) × (1 / GF)
```

where `GF` is the gage factor (typically 2.0) and `R_bridge` is the gage resistance (120 or 350 Ω).

**Required Inputs:** Excitation, gage factor, shunt Ω, bridge Ω, max load, quiescent load, Point 1 units

### 5.4 Two-Point Shunt (External)

Uses a WBK16/LC option board with an external shunt resistor. Two readings: unshunted and shunted.

### 5.5 Name Plate

For pre-calibrated transducers (load cells, pressure sensors) with known mV/V sensitivity.

**Required Inputs:** Excitation, sensitivity (mV/V), full rated load, max load, quiescent load

```
Full_scale_mV = sensitivity × excitation_V
m = Full_rated_load / Full_scale_mV
b = quiescent_load
```

---

## 6. Python Implementation

The DaqX API is a Windows DLL (`daqx.dll` or `daqx64.dll`). Python access is via `ctypes`. The implementation below provides a complete class that handles device discovery, channel configuration, calibration, acquisition, and teardown.

### 6.1 DaqX Constants

```python
"""
strainbook616.py

Complete Python driver for the IOtech/MCC StrainBook/616.
Uses the DaqX API via ctypes for Windows.

Requirements:
    - Windows OS with IOtech/MCC WaveBook software installed
    - daqx.dll (32-bit) or daqx64.dll (64-bit) on the system PATH
    - Python 3.10+, numpy

Usage:
    sb = StrainBook616("SB616-1")
    sb.configure_channel(0, bridge="full", excitation=10.0, gain=100)
    sb.configure_acquisition(rate=1000.0, num_scans=5000)
    data = sb.acquire()
    sb.close()
"""

import ctypes
import ctypes.wintypes as wt
import enum
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# DaqX API Constants (from daqx.h / Programmer's Manual p/n 1008-0901)
# ---------------------------------------------------------------------------

# Device types
DEV_STRAINBOOK616 = 0x0200

# Return codes
DaqError_NOERROR = 0

# Acquisition modes
class DaqAdcAcqMode(enum.IntEnum):
    ONESHOT  = 0    # Single burst acquisition
    CONTIN   = 1    # Continuous (streaming) acquisition

# Trigger sources
class DaqTrigSource(enum.IntEnum):
    SOFTWARE = 0    # Software (immediate) trigger
    TTLLOW   = 1    # TTL falling edge
    TTLHIGH  = 2    # TTL rising edge
    ANALOG   = 3    # Analog channel trigger

# Channel types for scan list
class DaqAdcChanType(enum.IntEnum):
    ANALOG      = 0     # Standard analog input
    LOCAL_DIGITAL = 1   # Digital I/O port
    COUNTER     = 2     # Counter input

# Gain codes (PGIA × PGA combined)
class DaqAdcGain(enum.IntEnum):
    X1       = 0
    X1_28    = 1
    X1_65    = 2
    X2_11    = 3
    X2_71    = 4
    X3_48    = 5
    X4_47    = 6
    X5_47    = 7
    X7_37    = 8
    X9_46    = 9
    X10      = 10
    X12_14   = 11
    X12_8    = 12
    X15_58   = 13
    X16_5    = 14
    X20      = 15
    X21_1    = 16
    X27_1    = 17
    X34_8    = 18
    X44_7    = 19
    X54_7    = 20
    X73_7    = 21
    X94_6    = 22
    X100     = 23
    X121_4   = 24
    X128     = 25
    X155_8   = 26
    X165     = 27
    X200     = 28
    X211     = 29
    X271     = 30
    X348     = 31
    X447     = 32
    X547     = 33
    X737     = 34
    X946     = 35
    X1000    = 36
    X1214    = 37
    X1280    = 38
    X1558    = 39
    X1650    = 40
    X2000    = 41
    X2110    = 42
    X2710    = 43
    X3480    = 44
    X4470    = 45
    X5470    = 46
    X7370    = 47
    X9460    = 48
    X10000   = 49
    X12140   = 50
    X15580   = 51
    X20000   = 52

# Bridge configuration
class DaqBridgeType(enum.IntEnum):
    FULL       = 0
    HALF       = 1
    QUARTER    = 2
    HIGHGAIN   = 3    # High-gain differential amplifier (no bridge)

# Excitation voltage
class DaqExcitation(enum.IntEnum):
    OFF   = 0
    V0_5  = 1
    V1_0  = 2
    V2_0  = 3
    V5_0  = 4
    V10_0 = 5

EXCITATION_VOLTS = {
    DaqExcitation.OFF:  0.0,
    DaqExcitation.V0_5: 0.5,
    DaqExcitation.V1_0: 1.0,
    DaqExcitation.V2_0: 2.0,
    DaqExcitation.V5_0: 5.0,
    DaqExcitation.V10_0: 10.0,
}

# Filter modes
class DaqFilterMode(enum.IntEnum):
    BYPASS  = 0
    LPF_10  = 1     # 10 Hz 4-pole Butterworth
    LPF_1K  = 2     # 1 kHz 4-pole Butterworth

# Coupling
class DaqCoupling(enum.IntEnum):
    DC = 0
    AC = 1

# Shunt calibration positions
class DaqShuntCal(enum.IntEnum):
    OFF = 0
    RB  = 1
    RD  = 2
    RF  = 3

# DaqX option types for daqSetOption / daqGetOption
class DaqOptionType(enum.IntEnum):
    BRIDGE_TYPE   = 0x1000
    EXCITATION    = 0x1001
    FILTER_MODE   = 0x1002
    COUPLING      = 0x1003
    SHUNT_CAL     = 0x1004
    INVERT        = 0x1005
    SSH_ENABLE    = 0x1006
    AUTO_ZERO     = 0x1007
```

### 6.2 Calibration Data Structures

```python
# ---------------------------------------------------------------------------
# Calibration data
# ---------------------------------------------------------------------------
@dataclass
class ChannelCalibration:
    """Per-channel calibration state."""
    method: str = "none"
    excitation_v: float = 10.0
    bridge_type: DaqBridgeType = DaqBridgeType.FULL

    # mX + b conversion: eng_units = scale * voltage + offset
    scale: float = 1.0          # m
    offset: float = 0.0         # b

    # Two-point manual / auto
    point1_mv: float = 0.0
    point1_units: float = 0.0
    point2_mv: float = 0.0
    point2_units: float = 0.0

    # Shunt cal
    gage_factor: float = 2.0
    shunt_ohms: float = 0.0
    bridge_ohms: float = 350.0

    # Nameplate
    sensitivity_mv_per_v: float = 0.0
    full_rated_load: float = 0.0

    # Range
    max_load: float = 1000.0
    quiescent_load: float = 0.0

    # Hardware gain settings determined during calibration
    pgia_gain: int = 1
    pga_gain: float = 1.0
    offset_dac_v: float = 0.0


@dataclass
class ChannelConfig:
    """Per-channel hardware configuration."""
    enabled: bool = True
    bridge_type: DaqBridgeType = DaqBridgeType.FULL
    excitation: DaqExcitation = DaqExcitation.V10_0
    gain: DaqAdcGain = DaqAdcGain.X100
    filter_mode: DaqFilterMode = DaqFilterMode.LPF_1K
    coupling: DaqCoupling = DaqCoupling.DC
    ssh: bool = True
    auto_zero: bool = True
    invert: bool = False
    label: str = ""
    units: str = "µε"
    cal: ChannelCalibration = field(default_factory=ChannelCalibration)
```

### 6.3 DaqX DLL Wrapper

```python
# ---------------------------------------------------------------------------
# DaqX DLL Wrapper
# ---------------------------------------------------------------------------
class DaqXLib:
    """Thin wrapper around the DaqX DLL functions."""

    def __init__(self):
        """Load the DaqX DLL."""
        import platform
        is_64 = platform.architecture()[0] == '64bit'
        dll_name = "daqx64.dll" if is_64 else "daqx.dll"

        try:
            self._dll = ctypes.WinDLL(dll_name)
        except OSError:
            # Try common install paths
            for search in [
                Path(r"C:\Program Files\IOtech\DAQSoftware"),
                Path(r"C:\Program Files (x86)\IOtech\DAQSoftware"),
                Path(r"C:\Program Files\Measurement Computing\DAQ"),
            ]:
                candidate = search / dll_name
                if candidate.exists():
                    self._dll = ctypes.WinDLL(str(candidate))
                    break
            else:
                raise FileNotFoundError(
                    f"Cannot find {dll_name}. Ensure IOtech/MCC software is installed."
                )

        self._setup_prototypes()

    def _setup_prototypes(self):
        """Define C function signatures for type safety."""
        dll = self._dll

        # daqOpen(deviceName) -> handle
        dll.daqOpen.argtypes = [ctypes.c_char_p]
        dll.daqOpen.restype = ctypes.c_int

        # daqClose(handle) -> error
        dll.daqClose.argtypes = [ctypes.c_int]
        dll.daqClose.restype = ctypes.c_int

        # daqInit(handle) -> error  (sometimes called daqAdcRd or reset)
        if hasattr(dll, 'daqOnline'):
            dll.daqOnline.argtypes = [ctypes.c_int]
            dll.daqOnline.restype = ctypes.c_int

        # daqAdcSetScan(handle, channels, gains, flags, chanCount)
        dll.daqAdcSetScan.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),   # channels[]
            ctypes.POINTER(ctypes.c_int),   # gains[]
            ctypes.POINTER(ctypes.c_int),   # flags[]
            ctypes.c_int,                   # chanCount
        ]
        dll.daqAdcSetScan.restype = ctypes.c_int

        # daqAdcSetAcq(handle, mode, preTrig, postTrig)
        dll.daqAdcSetAcq.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_uint, ctypes.c_uint
        ]
        dll.daqAdcSetAcq.restype = ctypes.c_int

        # daqAdcSetFreq(handle, freq)
        dll.daqAdcSetFreq.argtypes = [ctypes.c_int, ctypes.POINTER(ctypes.c_float)]
        dll.daqAdcSetFreq.restype = ctypes.c_int

        # daqAdcArm(handle)
        dll.daqAdcArm.argtypes = [ctypes.c_int]
        dll.daqAdcArm.restype = ctypes.c_int

        # daqAdcTransferSetBuffer(handle, buf, scanCount, ...)
        dll.daqAdcTransferSetBuffer.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_short),
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_int,
        ]
        dll.daqAdcTransferSetBuffer.restype = ctypes.c_int

        # daqAdcTransferStart(handle)
        dll.daqAdcTransferStart.argtypes = [ctypes.c_int]
        dll.daqAdcTransferStart.restype = ctypes.c_int

        # daqAdcTransferGetStat(handle, active, retCount)
        dll.daqAdcTransferGetStat.argtypes = [
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint),
        ]
        dll.daqAdcTransferGetStat.restype = ctypes.c_int

        # daqAdcDisarm(handle)
        dll.daqAdcDisarm.argtypes = [ctypes.c_int]
        dll.daqAdcDisarm.restype = ctypes.c_int

        # daqSetOption(handle, chan, optType, optValue)
        dll.daqSetOption.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int
        ]
        dll.daqSetOption.restype = ctypes.c_int

        # daqAdcSetTrig(handle, source, rising, level, chan, gainCode, flags)
        dll.daqAdcSetTrig.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
            ctypes.c_float, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        dll.daqAdcSetTrig.restype = ctypes.c_int

    def check(self, err_code: int, context: str = ""):
        """Raise on DaqX error."""
        if err_code != DaqError_NOERROR:
            raise RuntimeError(
                f"DaqX error {err_code} in {context}. "
                f"See Programmer's Manual for error code definitions."
            )
```

### 6.4 Main Driver Class

```python
# ---------------------------------------------------------------------------
# StrainBook/616 Driver
# ---------------------------------------------------------------------------
class StrainBook616:
    """
    Complete Python driver for the StrainBook/616.

    Mirrors the LabVIEW acquisition sequence:
        1. Open device (Locate Device By Name)
        2. Configure channels (SB616 Input Channel Configuration)
        3. Configure trigger (Basic Trigger Configuration)
        4. Configure scan (Acquisition Scan Configuration)
        5. Arm (Acquisition Arm)
        6. Read data (Acquisition Read Scan) — loop or single-shot
        7. Close (Acquisition Close)
    """

    NUM_CHANNELS = 8
    ADC_BITS = 16
    ADC_RANGE_V = 10.0          # ±10V full-scale at the A/D input
    MAX_SCAN_RATE = 1_000_000   # 1 MHz total scan rate

    def __init__(self, device_name: str = "SB616-1"):
        """
        Open a connection to the StrainBook.

        Args:
            device_name: Network name assigned via the Daq Configuration
                         Applet (e.g., "SB616-1"). Must match the name
                         in the Windows device table.
        """
        self.device_name = device_name
        self._lib = DaqXLib()
        self._handle: int = -1
        self.channels: list[ChannelConfig] = [
            ChannelConfig(label=f"CH{i+1}") for i in range(self.NUM_CHANNELS)
        ]
        self._scan_rate: float = 1000.0
        self._num_scans: int = 1000
        self._pre_trigger: int = 0
        self._armed: bool = False

        # Open device
        self._handle = self._lib._dll.daqOpen(device_name.encode('ascii'))
        if self._handle < 0:
            raise ConnectionError(
                f"Cannot open StrainBook '{device_name}'. "
                f"Verify device name in Daq Configuration Applet and "
                f"ensure Ethernet connection is active."
            )
        logger.info(f"Opened StrainBook '{device_name}', handle={self._handle}")

    # ── Channel Configuration ──────────────────────────────────────────

    def configure_channel(self, channel: int, *,
                          bridge: str = "full",
                          excitation: float = 10.0,
                          gain: int = 100,
                          filter_hz: Optional[float] = 1000.0,
                          coupling: str = "DC",
                          ssh: bool = True,
                          auto_zero: bool = True,
                          invert: bool = False,
                          units: str = "µε",
                          label: str = ""):
        """
        Configure a single StrainBook channel.

        Args:
            channel:     0-7 channel index
            bridge:      "full", "half", "quarter", or "highgain"
            excitation:  Excitation voltage (0, 0.5, 1.0, 2.0, 5.0, 10.0)
            gain:        Desired gain (nearest available will be selected)
            filter_hz:   Low-pass filter (10, 1000, or None for bypass)
            coupling:    "DC" or "AC"
            ssh:         Enable simultaneous sample-and-hold
            auto_zero:   Enable auto-zero (software offset nulling)
            invert:      Invert output polarity
            units:       Engineering units label
            label:       Channel label string
        """
        if not 0 <= channel < self.NUM_CHANNELS:
            raise ValueError(f"Channel must be 0–{self.NUM_CHANNELS-1}")

        cfg = self.channels[channel]
        cfg.enabled = True
        cfg.label = label or f"CH{channel+1}"
        cfg.units = units

        # Bridge type
        bridge_map = {
            "full": DaqBridgeType.FULL,
            "half": DaqBridgeType.HALF,
            "quarter": DaqBridgeType.QUARTER,
            "highgain": DaqBridgeType.HIGHGAIN,
        }
        cfg.bridge_type = bridge_map.get(bridge.lower(), DaqBridgeType.FULL)

        # Excitation
        exc_map = {0: DaqExcitation.OFF, 0.5: DaqExcitation.V0_5,
                   1.0: DaqExcitation.V1_0, 2.0: DaqExcitation.V2_0,
                   5.0: DaqExcitation.V5_0, 10.0: DaqExcitation.V10_0}
        cfg.excitation = exc_map.get(excitation, DaqExcitation.V10_0)

        # Gain (find nearest)
        cfg.gain = self._nearest_gain(gain)

        # Filter
        if filter_hz is None or filter_hz == 0:
            cfg.filter_mode = DaqFilterMode.BYPASS
        elif filter_hz <= 100:
            cfg.filter_mode = DaqFilterMode.LPF_10
        else:
            cfg.filter_mode = DaqFilterMode.LPF_1K

        cfg.coupling = DaqCoupling.AC if coupling.upper() == "AC" else DaqCoupling.DC
        cfg.ssh = ssh
        cfg.auto_zero = auto_zero
        cfg.invert = invert

        # Write to hardware
        self._apply_channel_config(channel)

    def _apply_channel_config(self, ch: int):
        """Write channel config to the StrainBook via DaqX options."""
        cfg = self.channels[ch]
        lib = self._lib

        opts = [
            (DaqOptionType.BRIDGE_TYPE, cfg.bridge_type),
            (DaqOptionType.EXCITATION, cfg.excitation),
            (DaqOptionType.FILTER_MODE, cfg.filter_mode),
            (DaqOptionType.COUPLING, cfg.coupling),
            (DaqOptionType.SSH_ENABLE, int(cfg.ssh)),
            (DaqOptionType.AUTO_ZERO, int(cfg.auto_zero)),
            (DaqOptionType.INVERT, int(cfg.invert)),
        ]
        for opt_type, opt_val in opts:
            err = lib._dll.daqSetOption(self._handle, ch, int(opt_type), opt_val)
            lib.check(err, f"daqSetOption ch={ch} opt=0x{int(opt_type):04X}")

    @staticmethod
    def _nearest_gain(target: float) -> DaqAdcGain:
        """Find the DaqAdcGain enum closest to the requested gain value."""
        gain_table = [
            (1, DaqAdcGain.X1), (1.28, DaqAdcGain.X1_28),
            (1.65, DaqAdcGain.X1_65), (2.11, DaqAdcGain.X2_11),
            (2.71, DaqAdcGain.X2_71), (3.48, DaqAdcGain.X3_48),
            (4.47, DaqAdcGain.X4_47), (5.47, DaqAdcGain.X5_47),
            (7.37, DaqAdcGain.X7_37), (9.46, DaqAdcGain.X9_46),
            (10, DaqAdcGain.X10), (12.14, DaqAdcGain.X12_14),
            (12.8, DaqAdcGain.X12_8), (15.58, DaqAdcGain.X15_58),
            (16.5, DaqAdcGain.X16_5), (20, DaqAdcGain.X20),
            (21.1, DaqAdcGain.X21_1), (27.1, DaqAdcGain.X27_1),
            (34.8, DaqAdcGain.X34_8), (44.7, DaqAdcGain.X44_7),
            (54.7, DaqAdcGain.X54_7), (73.7, DaqAdcGain.X73_7),
            (94.6, DaqAdcGain.X94_6), (100, DaqAdcGain.X100),
            (121.4, DaqAdcGain.X121_4), (128, DaqAdcGain.X128),
            (155.8, DaqAdcGain.X155_8), (165, DaqAdcGain.X165),
            (200, DaqAdcGain.X200), (211, DaqAdcGain.X211),
            (271, DaqAdcGain.X271), (348, DaqAdcGain.X348),
            (447, DaqAdcGain.X447), (547, DaqAdcGain.X547),
            (737, DaqAdcGain.X737), (946, DaqAdcGain.X946),
            (1000, DaqAdcGain.X1000), (1214, DaqAdcGain.X1214),
            (1280, DaqAdcGain.X1280), (1558, DaqAdcGain.X1558),
            (1650, DaqAdcGain.X1650), (2000, DaqAdcGain.X2000),
            (2110, DaqAdcGain.X2110), (2710, DaqAdcGain.X2710),
            (3480, DaqAdcGain.X3480), (4470, DaqAdcGain.X4470),
            (5470, DaqAdcGain.X5470), (7370, DaqAdcGain.X7370),
            (9460, DaqAdcGain.X9460), (10000, DaqAdcGain.X10000),
            (12140, DaqAdcGain.X12140), (15580, DaqAdcGain.X15580),
            (20000, DaqAdcGain.X20000),
        ]
        best = min(gain_table, key=lambda g: abs(g[0] - target))
        return best[1]

    # ── Calibration ────────────────────────────────────────────────────

    def calibrate_nameplate(self, channel: int, *,
                            sensitivity_mv_per_v: float,
                            full_rated_load: float,
                            max_load: float,
                            quiescent_load: float = 0.0):
        """
        Name Plate calibration — for load cells and pre-calibrated transducers.

        Uses the sensor's rated sensitivity (mV/V) to compute conversion
        factors without taking any physical measurements.
        """
        cfg = self.channels[channel]
        cal = cfg.cal
        cal.method = "nameplate"
        cal.excitation_v = EXCITATION_VOLTS[cfg.excitation]
        cal.sensitivity_mv_per_v = sensitivity_mv_per_v
        cal.full_rated_load = full_rated_load
        cal.max_load = max_load
        cal.quiescent_load = quiescent_load

        # Full-scale output in mV
        full_scale_mv = sensitivity_mv_per_v * cal.excitation_v

        # scale: engineering units per mV of bridge output
        cal.scale = full_rated_load / full_scale_mv if full_scale_mv != 0 else 1.0
        cal.offset = quiescent_load

        logger.info(
            f"CH{channel}: Nameplate cal — "
            f"m={cal.scale:.4f} units/mV, b={cal.offset:.2f}"
        )

    def calibrate_two_point_auto(self, channel: int, *,
                                  point1_mv: float, point1_units: float,
                                  point2_mv: float, point2_units: float,
                                  max_load: float, quiescent_load: float = 0.0):
        """
        Two-Point Automatic calibration — no physical loads needed.
        User supplies two known (mV, engineering_units) pairs.
        """
        cal = self.channels[channel].cal
        cal.method = "2pt_auto"
        cal.point1_mv = point1_mv
        cal.point1_units = point1_units
        cal.point2_mv = point2_mv
        cal.point2_units = point2_units
        cal.max_load = max_load
        cal.quiescent_load = quiescent_load

        dmv = point2_mv - point1_mv
        if dmv == 0:
            raise ValueError("Two-point calibration requires different mV values")

        cal.scale = (point2_units - point1_units) / dmv
        cal.offset = point1_units - cal.scale * point1_mv

        logger.info(f"CH{channel}: 2-pt auto cal — m={cal.scale:.6f}, b={cal.offset:.4f}")

    def calibrate_two_point_manual(self, channel: int, *,
                                    point1_units: float, point2_units: float,
                                    max_load: float, quiescent_load: float = 0.0,
                                    read_func=None):
        """
        Two-Point Manual calibration — applies two known loads and
        measures the corresponding bridge voltages.

        Args:
            read_func: Callable that returns a single voltage reading (mV)
                       for the given channel. If None, uses _read_single().
        """
        cal = self.channels[channel].cal
        cal.method = "2pt_manual"
        cal.max_load = max_load
        cal.quiescent_load = quiescent_load

        reader = read_func or (lambda: self._read_single_mv(channel))

        # Point 1
        input(f"  Apply load of {point1_units} {self.channels[channel].units} "
              f"to CH{channel} and press Enter...")
        v1 = reader()
        cal.point1_mv = v1
        cal.point1_units = point1_units
        print(f"    Read: {v1:.4f} mV")

        # Point 2
        input(f"  Apply load of {point2_units} {self.channels[channel].units} "
              f"to CH{channel} and press Enter...")
        v2 = reader()
        cal.point2_mv = v2
        cal.point2_units = point2_units
        print(f"    Read: {v2:.4f} mV")

        dmv = v2 - v1
        if abs(dmv) < 1e-9:
            raise ValueError("Calibration failed: no voltage change between points")

        cal.scale = (point2_units - point1_units) / dmv
        cal.offset = point1_units - cal.scale * v1

        logger.info(f"CH{channel}: 2-pt manual cal — m={cal.scale:.6f}, b={cal.offset:.4f}")

    def calibrate_shunt(self, channel: int, *,
                        gage_factor: float = 2.0,
                        shunt_ohms: float,
                        bridge_ohms: float = 350.0,
                        max_load: float,
                        quiescent_load: float = 0.0,
                        point1_units: float = 0.0,
                        shunt_position: DaqShuntCal = DaqShuntCal.RB):
        """
        Internal Shunt calibration.

        Calculates expected microstrain from shunt resistance, then takes
        two readings (unshunted and shunted) to derive scale/offset.
        """
        cal = self.channels[channel].cal
        cal.method = "shunt"
        cal.gage_factor = gage_factor
        cal.shunt_ohms = shunt_ohms
        cal.bridge_ohms = bridge_ohms
        cal.max_load = max_load
        cal.quiescent_load = quiescent_load

        # Expected microstrain from shunt
        epsilon_shunt = -(bridge_ohms / (shunt_ohms + bridge_ohms)) / gage_factor
        epsilon_shunt_ue = epsilon_shunt * 1e6  # convert to µε

        # Read unshunted
        self._set_shunt_cal(channel, DaqShuntCal.OFF)
        time.sleep(0.2)
        v_unshunted = self._read_single_mv(channel)

        # Read shunted
        self._set_shunt_cal(channel, shunt_position)
        time.sleep(0.2)
        v_shunted = self._read_single_mv(channel)

        # Restore
        self._set_shunt_cal(channel, DaqShuntCal.OFF)

        dmv = v_shunted - v_unshunted
        if abs(dmv) < 1e-9:
            raise ValueError("Shunt cal failed: no voltage change")

        cal.scale = epsilon_shunt_ue / dmv
        cal.offset = point1_units - cal.scale * v_unshunted

        logger.info(
            f"CH{channel}: Shunt cal — ε_shunt={epsilon_shunt_ue:.1f} µε, "
            f"m={cal.scale:.6f}, b={cal.offset:.4f}"
        )

    def _set_shunt_cal(self, channel: int, position: DaqShuntCal):
        """Activate/deactivate a shunt calibration resistor."""
        err = self._lib._dll.daqSetOption(
            self._handle, channel, int(DaqOptionType.SHUNT_CAL), int(position)
        )
        self._lib.check(err, f"shunt_cal ch={channel}")

    def auto_zero(self, channels: Optional[list[int]] = None):
        """
        Auto-zero (auto-balance) the specified channels.
        Removes the static DC offset and zeros the input.
        """
        targets = channels or list(range(self.NUM_CHANNELS))
        for ch in targets:
            if self.channels[ch].enabled:
                err = self._lib._dll.daqSetOption(
                    self._handle, ch, int(DaqOptionType.AUTO_ZERO), 1
                )
                self._lib.check(err, f"auto_zero ch={ch}")
        logger.info(f"Auto-zeroed channels: {targets}")

    # ── Acquisition Configuration ──────────────────────────────────────

    def configure_acquisition(self, *,
                               rate: float = 1000.0,
                               num_scans: int = 1000,
                               pre_trigger: int = 0,
                               mode: DaqAdcAcqMode = DaqAdcAcqMode.ONESHOT,
                               trigger: DaqTrigSource = DaqTrigSource.SOFTWARE):
        """
        Configure the acquisition parameters.

        Args:
            rate:        Scan rate in Hz (per channel)
            num_scans:   Total scans to acquire (post-trigger)
            pre_trigger: Pre-trigger scans
            mode:        ONESHOT or CONTIN
            trigger:     SOFTWARE, TTLLOW, TTLHIGH, or ANALOG
        """
        self._scan_rate = rate
        self._num_scans = num_scans
        self._pre_trigger = pre_trigger

        # Build scan list from enabled channels
        enabled = [i for i in range(self.NUM_CHANNELS) if self.channels[i].enabled]
        n_ch = len(enabled)
        if n_ch == 0:
            raise ValueError("No channels enabled")

        total_rate = rate * n_ch
        if total_rate > self.MAX_SCAN_RATE:
            raise ValueError(
                f"Total scan rate {total_rate/1e6:.3f} MHz exceeds 1 MHz limit. "
                f"Reduce per-channel rate or number of channels."
            )

        # daqAdcSetScan
        ch_arr = (ctypes.c_int * n_ch)(*enabled)
        gain_arr = (ctypes.c_int * n_ch)(
            *[int(self.channels[i].gain) for i in enabled]
        )
        flag_arr = (ctypes.c_int * n_ch)(
            *[int(DaqAdcChanType.ANALOG)] * n_ch
        )
        err = self._lib._dll.daqAdcSetScan(
            self._handle, ch_arr, gain_arr, flag_arr, n_ch
        )
        self._lib.check(err, "daqAdcSetScan")

        # daqAdcSetAcq
        err = self._lib._dll.daqAdcSetAcq(
            self._handle, int(mode), pre_trigger, num_scans
        )
        self._lib.check(err, "daqAdcSetAcq")

        # daqAdcSetFreq
        freq = ctypes.c_float(rate)
        err = self._lib._dll.daqAdcSetFreq(self._handle, ctypes.byref(freq))
        self._lib.check(err, "daqAdcSetFreq")
        self._scan_rate = freq.value  # actual rate may differ

        # daqAdcSetTrig
        err = self._lib._dll.daqAdcSetTrig(
            self._handle, int(trigger), 1, 0.0, 0, 0, 0
        )
        self._lib.check(err, "daqAdcSetTrig")

        logger.info(
            f"Acquisition configured: {n_ch} ch × {self._scan_rate:.1f} Hz "
            f"= {n_ch * self._scan_rate / 1e3:.1f} kHz total, "
            f"{num_scans} scans, trigger={trigger.name}"
        )

    # ── Data Acquisition ───────────────────────────────────────────────

    def acquire(self) -> np.ndarray:
        """
        Execute a complete acquisition cycle: arm → trigger → read → disarm.

        Returns:
            2D numpy array of shape (num_scans, num_enabled_channels)
            in engineering units (after mX+b calibration).
        """
        enabled = [i for i in range(self.NUM_CHANNELS) if self.channels[i].enabled]
        n_ch = len(enabled)
        n_scans = self._num_scans + self._pre_trigger
        total_samples = n_scans * n_ch

        # Allocate buffer
        buf = (ctypes.c_short * total_samples)()

        # Set transfer buffer
        err = self._lib._dll.daqAdcTransferSetBuffer(
            self._handle, buf, ctypes.c_uint(n_scans), 0, 0
        )
        self._lib.check(err, "daqAdcTransferSetBuffer")

        # Arm
        err = self._lib._dll.daqAdcArm(self._handle)
        self._lib.check(err, "daqAdcArm")
        self._armed = True

        # Start transfer (software trigger fires automatically for SOFTWARE mode)
        err = self._lib._dll.daqAdcTransferStart(self._handle)
        self._lib.check(err, "daqAdcTransferStart")

        # Poll for completion
        active = ctypes.c_int(1)
        ret_count = ctypes.c_uint(0)
        timeout = time.time() + (n_scans / max(self._scan_rate, 1)) + 10.0

        while active.value != 0:
            err = self._lib._dll.daqAdcTransferGetStat(
                self._handle, ctypes.byref(active), ctypes.byref(ret_count)
            )
            self._lib.check(err, "daqAdcTransferGetStat")
            if time.time() > timeout:
                self._lib._dll.daqAdcDisarm(self._handle)
                raise TimeoutError("Acquisition timed out")
            time.sleep(0.01)

        # Disarm
        self._lib._dll.daqAdcDisarm(self._handle)
        self._armed = False

        # Convert raw buffer to numpy
        raw = np.frombuffer(buf, dtype=np.int16).reshape(n_scans, n_ch).astype(np.float64)

        # Apply mX+b calibration per channel
        result = np.empty_like(raw)
        for col, ch_idx in enumerate(enabled):
            cal = self.channels[ch_idx].cal
            # Convert raw 16-bit → voltage at ADC input
            voltage_v = raw[:, col] / 32768.0 * self.ADC_RANGE_V
            # Convert voltage to mV at bridge output (undo gain)
            gain_val = self._gain_numeric(self.channels[ch_idx].gain)
            bridge_mv = voltage_v / gain_val * 1000.0
            # Apply mX+b
            result[:, col] = cal.scale * bridge_mv + cal.offset

        return result

    def _read_single_mv(self, channel: int) -> float:
        """Take a single reading from one channel, return bridge mV."""
        # Save current config, do a 1-scan acquisition on just this channel
        orig_enabled = [ch.enabled for ch in self.channels]
        for i in range(self.NUM_CHANNELS):
            self.channels[i].enabled = (i == channel)

        self.configure_acquisition(rate=1000.0, num_scans=10)
        data = self.acquire()

        # Restore
        for i, en in enumerate(orig_enabled):
            self.channels[i].enabled = en

        # Average the readings, return as mV
        # data is already in engineering units; for raw mV we need to undo cal
        cal = self.channels[channel].cal
        avg_eu = float(np.mean(data[:, 0]))
        # Invert mX+b: mV = (eu - b) / m
        if abs(cal.scale) > 1e-12:
            return (avg_eu - cal.offset) / cal.scale
        return avg_eu

    @staticmethod
    def _gain_numeric(gain_code: DaqAdcGain) -> float:
        """Convert a DaqAdcGain enum to its numeric gain value."""
        table = {
            DaqAdcGain.X1: 1, DaqAdcGain.X1_28: 1.28, DaqAdcGain.X1_65: 1.65,
            DaqAdcGain.X2_11: 2.11, DaqAdcGain.X2_71: 2.71, DaqAdcGain.X3_48: 3.48,
            DaqAdcGain.X4_47: 4.47, DaqAdcGain.X5_47: 5.47, DaqAdcGain.X7_37: 7.37,
            DaqAdcGain.X9_46: 9.46, DaqAdcGain.X10: 10, DaqAdcGain.X100: 100,
            DaqAdcGain.X1000: 1000, DaqAdcGain.X10000: 10000, DaqAdcGain.X20000: 20000,
        }
        return table.get(gain_code, 1.0)

    # ── Cleanup ────────────────────────────────────────────────────────

    def close(self):
        """Close the device handle and release resources."""
        if self._armed:
            self._lib._dll.daqAdcDisarm(self._handle)
        if self._handle >= 0:
            self._lib._dll.daqClose(self._handle)
            logger.info(f"Closed StrainBook '{self.device_name}'")
            self._handle = -1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def __del__(self):
        self.close()
```

### 6.5 Example: Full Acquisition Pipeline

```python
def example_strain_acquisition():
    """
    Complete example: configure 4 channels of quarter-bridge strain gages,
    calibrate using nameplate method, acquire 5 seconds of data at 1 kHz.
    """
    with StrainBook616("SB616-1") as sb:

        # Configure channels 0-3: 350Ω quarter-bridge, 5V excitation
        for ch in range(4):
            sb.configure_channel(ch,
                bridge="quarter",
                excitation=5.0,
                gain=1000,
                filter_hz=1000,
                coupling="DC",
                ssh=True,
                units="µε",
                label=f"SG-{ch+1}",
            )

        # Disable unused channels
        for ch in range(4, 8):
            sb.channels[ch].enabled = False

        # Calibrate: 350Ω quarter-bridge gages, GF=2.09
        for ch in range(4):
            sb.calibrate_two_point_auto(ch,
                point1_mv=0.0, point1_units=0.0,
                point2_mv=5.225,   # 5V × 2.09 × 500µε / 4 ≈ 2.6 mV
                point2_units=500.0,
                max_load=2000.0,
                quiescent_load=0.0,
            )

        # Auto-zero all active channels
        sb.auto_zero(channels=[0, 1, 2, 3])

        # Acquire 5 seconds at 1 kHz
        sb.configure_acquisition(
            rate=1000.0,
            num_scans=5000,
            trigger=DaqTrigSource.SOFTWARE,
        )

        print("Acquiring...")
        data = sb.acquire()

        # data shape: (5000, 4) in µε
        print(f"Data shape: {data.shape}")
        print(f"Channel means: {data.mean(axis=0)}")
        print(f"Channel stdev: {data.std(axis=0)}")

        # Save to CSV
        import csv
        with open("strain_data.csv", "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["Scan", "SG-1 (µε)", "SG-2 (µε)",
                           "SG-3 (µε)", "SG-4 (µε)"])
            for i, row in enumerate(data):
                writer.writerow([i] + [f"{v:.2f}" for v in row])

        print("Saved to strain_data.csv")


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO)
    example_strain_acquisition()
```

---

## 7. Bridge Configuration Reference

### Full Bridge (4-wire)

```
        +Exc ──────┬──── R1 (gage) ───┬──── +Sig
                   │                  │
                   R4 (gage)          R2 (gage)
                   │                  │
        -Exc ──────┴──── R3 (gage) ───┴──── -Sig
```

- Sensitivity: `V_out = V_exc × GF × ε` (all 4 active arms)
- No bridge completion resistors needed
- Highest sensitivity, best temperature compensation

### Half Bridge

```
        +Exc ──────┬──── R1 (gage) ───┬──── +Sig
                   │                  │
                   R_comp (internal)  R2 (gage)
                   │                  │
        -Exc ──────┴──── R_comp ──────┴──── -Sig
```

- Uses 2 internal completion resistors (user-installed on CN-115 header)
- Sensitivity: `V_out = V_exc × GF × ε / 2`

### Quarter Bridge (3-wire)

```
        +Exc ──────┬──── R_gage ──────┬──── +Sig
                   │                  │
                   R_comp             R_comp
                   │                  │
        -Exc ──────┴──── R_comp ──────┴──── -Sig
```

- Uses 3 internal completion resistors
- Third wire for lead compensation
- Sensitivity: `V_out = V_exc × GF × ε / 4`
- Remote sense can linearize the quarter-bridge

---

## 8. Operational Notes for Wind Tunnel Use

**Excitation current budget.** At 10V excitation, a 120Ω full bridge draws 83 mA, near the 85 mA/channel foldback limit. Add internal reference node resistors (recommended: 1000Ω instead of 120Ω) and you risk overloading the regulator. For 120Ω gages, use 5V excitation or lower to stay safely below the limit. At 350Ω, 10V is fine (draws 28.6 mA).

**Warm-up.** The unit needs 30 minutes to reach rated specification accuracy. Plan your calibration after warm-up, not before.

**Auto-zero timing.** Auto-zero compensates for DC drift but should be performed after thermal equilibrium. In a wind tunnel with changing ambient temperature, re-zero between run sets.

**SSH mode.** Always enable SSH (simultaneous sample-and-hold) for force balance measurements where phase coherence between channels matters. With SSH on 8 channels, the max rate drops from 125 kHz to 111 kHz per channel — the extra sample slot is consumed by the SSH aperture.

**Filter selection.** For quasi-static strain measurements (alpha sweeps), the 10 Hz filter is appropriate. For dynamic measurements (buffet, flutter), use 1 kHz or bypass. The physical filter resistor packs on the PCB must match the software selection.

**Shunt cal resistor values.** For a 350Ω quarter bridge with GF=2.0 and a target of ~1000 µε, the shunt resistor is approximately: `R_shunt = R_bridge / (GF × ε) − R_bridge = 350 / (2 × 0.001) − 350 = 174,650 Ω`. Use precision (0.01%) resistors for best accuracy.

---

## 9. Integration with Streamlined DAQ

When integrating the StrainBook/616 into the `streamlined.py` DAQ framework alongside the DAQBook 2000 and Delta C-2000:

1. **Device discovery**: The StrainBook uses the IOtech network device name (assigned via the Daq Configuration Applet) rather than a COM port or bus address. Ensure the device name is configured and the unit is reachable on the local Ethernet segment.

2. **Shared DaqX DLL**: The StrainBook and DAQBook 2000 both use the same `daqx.dll` library. You can hold multiple device handles open simultaneously — each `daqOpen()` call returns an independent handle.

3. **Clock synchronization**: If the StrainBook needs to be scan-synchronized with a DAQBook, use the SYNC port (not Ethernet). Otherwise, use software triggering and accept the ~100 µs latency.

4. **Data alignment**: The StrainBook's FIFO returns interleaved channel data in scan order. Reshape to `(n_scans, n_channels)` before merging with data from other devices. Timestamp alignment should use the scan counter, not wall-clock time.

5. **HDF5 storage**: In the streamlined.py HDF5 schema, StrainBook data belongs under a device group with datasets per channel, matching the `{FACILITY}_{MODEL}_{CONFIG}_{RUN}_{TIMESTAMP}` naming convention.
