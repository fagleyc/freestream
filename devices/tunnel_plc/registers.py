"""Register map for the Red Lion G315 Modbus TCP gateway (192.168.1.50).

The G315 bridges two GE PLCs (VersaMax over Ethernet SRTP, FanDrive over
RS-485 SNP) and re-exports the tags below as Crimson 32-bit "L4" gateway
blocks over its Modbus TCP slave (port 502, unit 1). Each L4 element is
TWO 16-bit holding registers; element N lives at protocol address
2*(N-1).

Authoritative source: the Crimson mapping exports in this directory
(``read_mappings.txt`` / ``write_mappings.txt``, from
SSWT_Logger_G315_v2). Block1 contains ONLY the RPM pair plus button/light
booleans — the analog channels (pressures, temperatures) are NOT in the
gateway. ``BLOCK1_TAGS`` is the ordered extension point (order ==
element order): the first planned extension is the bearing-temperature
analogs (``BEARING_TAGS``, elements 17–19), served only after the
Crimson block is extended and ``TunnelConfig.bearing_temps`` is on.

32-bit word order within an element pair is a Crimson driver setting we
cannot see in the export — it is configurable here (``TunnelConfig.
word_order``) and must be locked in from a live read of a known nonzero
value (a boolean 1 puts its bit in the LOW word).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

# ── Block1: READ-ONLY, elements 1..16 → protocol address 0, 32 registers ──
BLOCK1_ADDR = 0
# (tag name from the export, snapshot attribute, is_boolean)
BLOCK1_TAGS: List[Tuple[str, str, bool]] = [
    ("RPM_Set",                  "rpm_set",                  False),
    ("Actual_RPM",               "actual_rpm",               False),
    ("Tunnel_Fan_Stop_Button",   "tunnel_fan_stop_button",   True),
    ("Tunnel_Fan_Start_Button",  "tunnel_fan_start_button",  True),
    ("Cooling_Fan_Start_Button", "cooling_fan_start_button", True),
    ("Cooling_Fan_Stop_Button",  "cooling_fan_stop_button",  True),
    ("Bearing_Heater_On_Button", "bearing_heater_on",        True),
    ("Bearing_Temp_Low_Light",   "bearing_temp_low",         True),
    ("Fan_Running_Light",        "fan_running",              True),
    ("Console_Control_Light",    "console_control",          True),
    ("Oil_Level_Low_Light",      "oil_level_low",            True),
    ("Inverter_Fault_Light",     "inverter_fault",           True),
    ("Tunnel_Fan_Light_Start",   "tunnel_fan_light_start",   True),
    ("Tunnel_Fan_Light_Stop",    "tunnel_fan_light_stop",    True),
    ("Cooling_Fan_Light_Start",  "cooling_fan_light_start",  True),
    ("Cooling_Fan_Light_Stop",   "cooling_fan_light_stop",   True),
]
BLOCK1_ELEMENTS = len(BLOCK1_TAGS)          # 16 elements = 32 registers
BLOCK1_REGISTERS = BLOCK1_ELEMENTS * 2

# ── Block1 EXTENSION: bearing temperature analogs (opt-in) ──────────────
# The analog bearing sensors are VersaMax AI0007..AI0009
# (Analog_Feedback.B1/B2/B3) and are NOT in the shipped Crimson gateway
# database — only the Bearing_Temp_Low boolean is. Once the Crimson read
# block is extended with elements 17/18/19 mapped to these tags (see
# README), set ``TunnelConfig.bearing_temps = True``: the Block1 poll
# then reads elements 1..19 (protocol addresses 32/34/36 for the new
# elements) as ONE contiguous 38-register FC3 read. This list is the
# ordered extension of BLOCK1_TAGS — element order == list order.
BEARING_TAGS: List[Tuple[str, str]] = [
    ("Analog_Feedback.B1", "bearing_b1"),    # element 17 @ address 32
    ("Analog_Feedback.B2", "bearing_b2"),    # element 18 @ address 34
    ("Analog_Feedback.B3", "bearing_b3"),    # element 19 @ address 36
]
BLOCK1_ELEMENTS_EXT = BLOCK1_ELEMENTS + len(BEARING_TAGS)   # 19 elements
BLOCK1_REGISTERS_EXT = BLOCK1_ELEMENTS_EXT * 2              # 38 registers

# Raw-count → engineering scaling per channel, straight from
# tunnel_tags.csv: (raw_lo, raw_hi, eng_lo, eng_hi), linear.
BEARING_CAL: Dict[str, Tuple[float, float, float, float]] = {
    "bearing_b1": (955.0, 5035.0, 0.0, 150.0),
    "bearing_b2": (969.0, 4979.0, 0.0, 150.0),
    "bearing_b3": (930.0, 4994.0, 0.0, 150.0),
}


def scale_bearing(raw: float,
                  cal: Tuple[float, float, float, float]) -> float:
    """Linear raw-count → engineering value for one bearing channel."""
    raw_lo, raw_hi, eng_lo, eng_hi = cal
    return eng_lo + (float(raw) - raw_lo) * (eng_hi - eng_lo) \
        / (raw_hi - raw_lo)


def unscale_bearing(value: float,
                    cal: Tuple[float, float, float, float]) -> int:
    """Engineering value → raw counts (emulator / round-trip tests)."""
    raw_lo, raw_hi, eng_lo, eng_hi = cal
    return int(round(raw_lo + (float(value) - eng_lo)
                     * (raw_hi - raw_lo) / (eng_hi - eng_lo)))

# ── Block2: WRITE, elements 101..105 → protocol addresses 200..208 ──────
BLOCK2_ADDR: Dict[str, int] = {
    "Tunnel_Fan_Start_Button":  200,   # element 101
    "Tunnel_Fan_Stop_Button":   202,   # element 102
    "Cooling_Fan_Start_Button": 204,   # element 103
    "Cooling_Fan_Stop_Button":  206,   # element 104
    "RPM_Set":                  208,   # element 105
}


def element_addr(element: int) -> int:
    """Protocol address of Crimson L4 gateway element N (1-based)."""
    return 2 * (element - 1)


def decode_u32(lo_reg: int, hi_reg: int, word_order: str) -> int:
    """One L4 element from its two registers, signed 32-bit.

    ``word_order`` is "low_first" (regs[0] = low word) or "high_first".
    """
    if word_order == "low_first":
        v = (hi_reg << 16) | lo_reg
    elif word_order == "high_first":
        v = (lo_reg << 16) | hi_reg
    else:
        raise ValueError(f"word_order {word_order!r}")
    return v - 0x1_0000_0000 if v >= 0x8000_0000 else v


def encode_u32(value: int, word_order: str) -> Tuple[int, int]:
    """Two registers for one L4 element (inverse of :func:`decode_u32`)."""
    v = int(value) & 0xFFFF_FFFF
    lo, hi = v & 0xFFFF, (v >> 16) & 0xFFFF
    if word_order == "low_first":
        return lo, hi
    if word_order == "high_first":
        return hi, lo
    raise ValueError(f"word_order {word_order!r}")


def decode_block1(regs: List[int], word_order: str,
                  rpm_scale: float = 1.0,
                  bearing_cal: Optional[Dict[str, tuple]] = None
                  ) -> Dict[str, object]:
    """Raw Block1 registers → {snapshot attribute: value}.

    Numeric tags are scaled by ``rpm_scale`` (Crimson may serve RPM ×10
    if the tag's display decimal is baked in — verify live); booleans
    are ``value != 0``.

    When ``regs`` carries the EXTENDED block (38 registers, elements
    17–19 = Analog_Feedback.B1/B2/B3), the bearing channels decode too:
    scaled floats via ``bearing_cal`` (attr → (raw_lo, raw_hi, eng_lo,
    eng_hi); defaults to the tunnel_tags.csv constants in
    :data:`BEARING_CAL`). With the 16-element default read the bearing
    attributes are simply absent (snapshot keeps them ``None``).
    """
    if len(regs) < BLOCK1_REGISTERS:
        raise ValueError(f"need {BLOCK1_REGISTERS} registers, "
                         f"got {len(regs)}")
    out: Dict[str, object] = {}
    for i, (_tag, attr, is_bool) in enumerate(BLOCK1_TAGS):
        raw = decode_u32(regs[2 * i], regs[2 * i + 1], word_order)
        out[attr] = (raw != 0) if is_bool else raw * rpm_scale
    if len(regs) >= BLOCK1_REGISTERS_EXT:
        cal = bearing_cal or BEARING_CAL
        for j, (_tag, attr) in enumerate(BEARING_TAGS):
            i = BLOCK1_ELEMENTS + j
            raw = decode_u32(regs[2 * i], regs[2 * i + 1], word_order)
            out[attr] = scale_bearing(raw, cal[attr])
    return out


@dataclass
class TunnelSnapshot:
    """One atomic Block1 poll, engineering units + status booleans."""
    t: float = 0.0                     # time.time() of the poll
    stale: bool = True                 # set by the monitor
    age_s: float = float("inf")       # seconds since the poll succeeded

    rpm_set: float = 0.0
    actual_rpm: float = 0.0

    tunnel_fan_stop_button: bool = False
    tunnel_fan_start_button: bool = False
    cooling_fan_start_button: bool = False
    cooling_fan_stop_button: bool = False
    bearing_heater_on: bool = False
    bearing_temp_low: bool = False
    fan_running: bool = False
    console_control: bool = False
    oil_level_low: bool = False
    inverter_fault: bool = False
    tunnel_fan_light_start: bool = False
    tunnel_fan_light_stop: bool = False
    cooling_fan_light_start: bool = False
    cooling_fan_light_stop: bool = False

    # Bearing temperatures (extended Block1, opt-in via
    # TunnelConfig.bearing_temps) — None while the feature is disabled.
    bearing_b1: Optional[float] = None
    bearing_b2: Optional[float] = None
    bearing_b3: Optional[float] = None

    raw_registers: tuple = field(default=(), repr=False)
