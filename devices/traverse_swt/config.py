"""Runtime configuration for the SSWT traverse (WAGO 750 PLC).

One WAGO 750 controller (default **192.168.1.21:502**, Modbus unit 1)
drives three 750-673 stepper modules: X axial, Y lateral, Z vertical.
The PLC (CoDeSys project ``LVW_V3_2021.pro``) exposes a plain %MW map:

| %MW  | wire addr | meaning                                   |
|------|-----------|-------------------------------------------|
| MW0  | 12288     | ControlWord (host writes, see bit masks)  |
| MW1  | 12289     | StatusWord — limit bits 0/1/2 = X/Y/Z     |
| MW10 | 12298     | X position, DINT low-word-first           |
| MW12 | 12300     | Y position, DINT low-word-first           |
| MW14 | 12302     | Z position, DINT low-word-first           |

ControlWord bits (PLC source truth): X fwd/rev = bit0/bit1,
Y = bit2/bit3, Z = bit4/bit5.
NOTE: the deployed C# labeled Y and Z with fwd/rev SWAPPED vs the PLC
source names (C# "Y forward" wrote bit3, "Z forward" wrote bit5). The
default masks below reproduce the C# behavior — what operators are used
to — and ``fwd_increases_counts`` flips the wiring sense if the first
live test shows it is wrong.

Motion model: the PLC runs the steppers in velocity mode at a fixed
±2000 steps/s with accel/decel baked into the PLC program — the host has
NO speed control, only direction bits. Positioning is therefore a
host-side bang-bang loop: command the direction, watch the counts, drop
the bit inside the tolerance band.

Limits & homing (2026-07): the rig's limit switches work again
(inverted/fixed at the module) and land on StatusWord bits 0/1/2
(negative-direction switches). The module hardware-limit lockout is
UNLINKED (Ptr_LimitSwitch = 0) so the drive never stops itself — the
HOST watches the bit: it stops any move/jog that trips a limit, and
homing is a host-side jog-to-limit sequence (``device.home_axis``:
seek the negative limit → back off until the bit clears + a margin →
``calibrate_offset(home_datum_in)``). Homing is per-power-cycle: the
750-673 counter zeroes at module power-up, so re-home each setup.

Position calibration is the C# two-point form, in inches:
``inches = inch_high − (counts_high − counts) / clicks_per_inch``.
Slopes below come from the rig's ``sswtTraverseCalibrationFile.xml``
(signed: Y and Z counts DECREASE as inches increase). Axes start
``calibrated=False``; homing (or a manual re-zero) calibrates the offset.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import List, Optional


def defaults_path() -> Path:
    """Where "Set as Defaults" persists the startup config.

    Auto-loaded at every app launch (guarded — a parse error falls back
    to factory defaults). Overridable via the ``TRAVERSE_DEFAULTS`` env
    var (tests); default ``~/.traverse_swt/defaults.json``.
    """
    env = os.environ.get("TRAVERSE_DEFAULTS")
    return Path(env) if env else (Path.home() / ".traverse_swt" /
                                  "defaults.json")


@dataclass
class AxisConfig:
    """One traverse axis (one WAGO 750-673 stepper module)."""
    name: str = "X"
    label: str = "Axial"
    enabled: bool = True

    # ── ControlWord protocol ──
    fwd_mask: int = 0x0001          # ControlWord bit for "forward"
    rev_mask: int = 0x0002          # ControlWord bit for "reverse"
    position_addr: int = 12298      # wire address of the DINT low word

    # True → the fwd_mask direction increases the position counts.
    # Unverified on the rig; flip it if the first supervised jog shows
    # counts moving the wrong way (move_to also has a wrong-way trip).
    fwd_increases_counts: bool = True

    # The 750-673 modules are configured to roll their position counter
    # over cleanly at 1,000,000 counts (unsigned 0…999,999, wrapping
    # 999999→0 and 0→999999 — deliberate, so the module never reaches
    # the 24-bit counter limit where it used to stop stepping). The
    # driver unwraps modulo wrap_modulus into a continuous ABSOLUTE
    # position, so targets past 1M counts stay reachable; 0 disables
    # unwrapping (raw passthrough).
    wrap_modulus: int = 1_000_000

    # ── limit switch ──
    # limit_enabled: honor this axis's StatusWord limit bit (homing +
    # runtime reaction). X ships DISABLED per the rig (2026-07-22): the
    # axial limit input is ignored entirely.
    limit_enabled: bool = True

    # ── host-side homing (StatusWord limit bit + jog; device.home_axis) ──
    # home_enabled: this axis may be homed to its limit switch. X ships
    # disabled (no homing on the axial axis). home_datum_in is what the
    # limit position reads after homing (calibrate_offset is called
    # there). home_jog_fwd is the EXPLICIT, BIT-LEVEL seek direction:
    # True = the homing SEEK jogs this axis's fwd_mask ControlWord bit
    # (backoff jogs rev_mask), False = the opposite. It is deliberately
    # NOT derived from the slope/fwd_increases_counts bookkeeping —
    # position mode and homing proved to need independent senses on the
    # rig (2026-07-22), so each is set empirically on its own. The
    # runtime limit recovery direction is always the OPPOSITE bit.
    home_enabled: bool = False
    home_datum_in: float = -18.0
    home_jog_fwd: bool = True

    # ── two-point inches calibration ──
    inch_high: float = 0.0
    counts_high: int = 0
    clicks_per_inch: float = 13705.6
    calibrated: bool = False        # offset is per-power-cycle: re-zero

    # ── motion ──
    min_in: float = -5.0            # soft travel limits (inches)
    max_in: float = 5.0
    tolerance_in: float = 0.02      # move-complete band (C# default)

    def counts_to_inches(self, counts: int) -> float:
        return self.inch_high - ((self.counts_high - counts) /
                                 self.clicks_per_inch)

    def inches_to_counts(self, inches: float) -> int:
        return round(self.counts_high -
                     (self.inch_high - inches) * self.clicks_per_inch)

    # ── calibration routines ─────────────────────────────────────────────
    def calibrate_two_point(self, inch1: float, counts1: int,
                            inch2: float, counts2: int) -> float:
        """Full calibration: two (inches, counts) points → slope AND offset.

        Returns the computed clicks/inch (signed). Raises ValueError on
        degenerate points.
        """
        if abs(inch2 - inch1) < 1e-9:
            raise ValueError("calibration points must be at different "
                             "positions")
        if counts1 == counts2:
            raise ValueError("counts did not change between points")
        self.clicks_per_inch = (counts2 - counts1) / (inch2 - inch1)
        self.inch_high = float(inch2)
        self.counts_high = int(counts2)
        self.calibrated = True
        return self.clicks_per_inch

    def calibrate_offset(self, inches: float, counts: int) -> None:
        """Offset-only re-zero: current position = known inches.

        Keeps the existing slope (clicks/inch must already be valid).
        """
        if abs(self.clicks_per_inch) < 1e-9:
            raise ValueError("slope unknown — run the two-point "
                             "calibration first")
        self.inch_high = float(inches)
        self.counts_high = int(counts)
        self.calibrated = True


# Signed slopes derived from the rig's sswtTraverseCalibrationFile.xml
# cal points (the C# stored magnitudes and handled sign elsewhere):
#   X: (−5 in, −67383) → (+5 in, +69657)  ⇒ +13704 ≈ file's 13705.6
#   Y: (−5 in, +72240) → (+5 in, −76170)  ⇒ −14841.0
# Z sign convention set on the rig 2026-07-22 (supersedes the legacy XML
# sign): positive velocity / incrementing encoder moves the stage DOWN,
# and DOWN is the POSITIVE inches direction — so the slope is POSITIVE
# (+counts = +inches). The TOP is the negative direction: homing seeks
# −inches (decreasing counts, drive UP) to the top switch, datum −18".
X_CLICKS_PER_INCH = 13705.6
Y_CLICKS_PER_INCH = -14841.0
Z_CLICKS_PER_INCH = 986938.4


def _x() -> AxisConfig:
    # no homing on X: home_enabled stays False (home_axis("x") raises).
    # limit_enabled=False per rig 2026-07-22: the axial limit input is
    # DISABLED — its StatusWord bit is ignored entirely (no runtime
    # limit reaction on X).
    return AxisConfig(name="X", label="Axial",
                      fwd_mask=0x0001, rev_mask=0x0002,
                      position_addr=12298,
                      limit_enabled=False,
                      clicks_per_inch=X_CLICKS_PER_INCH,
                      min_in=-5.0, max_in=5.0)


def _y() -> AxisConfig:
    # C#-behavior masks: "forward" wrote bit3 (PLC names bit3 LateralJogRev)
    # Travel ±18" set from the rig (Casey 2026-07-22); homing datum −18.
    return AxisConfig(name="Y", label="Lateral",
                      fwd_mask=0x0008, rev_mask=0x0004,
                      position_addr=12300,
                      home_enabled=True, home_datum_in=-18.0,
                      clicks_per_inch=Y_CLICKS_PER_INCH,
                      min_in=-18.0, max_in=18.0)


def _z() -> AxisConfig:
    # C#-behavior masks: "forward" wrote bit5 (PLC names bit5 VerticalJogRev)
    # Rig 2026-07-22 (settled): POSITION mode is correct with the fwd-bit
    # inversion OFF (fwd_increases_counts=False), and HOMING is correct
    # with the OPPOSITE sense — so the homing direction is pinned at the
    # bit level, independent of the position bookkeeping: the seek jogs
    # the fwd_mask bit (home_jog_fwd=True, the "flopped" direction),
    # backoff jogs rev_mask. Datum +18" at the switch; soft limits ±25"
    # for now (final travel TBD on the rig).
    return AxisConfig(name="Z", label="Vertical",
                      fwd_mask=0x0020, rev_mask=0x0010,
                      position_addr=12302,
                      fwd_increases_counts=False,
                      home_enabled=True, home_datum_in=18.0,
                      home_jog_fwd=True,
                      clicks_per_inch=Z_CLICKS_PER_INCH,
                      min_in=-25.0, max_in=25.0)


@dataclass
class TraverseConfig:
    """All user-tunable settings for the SSWT traverse."""

    ip: str = "192.168.1.21"
    port: int = 502
    unit_id: int = 1

    x: AxisConfig = field(default_factory=_x)
    y: AxisConfig = field(default_factory=_y)
    z: AxisConfig = field(default_factory=_z)

    # ── control loop ─────────────────────────────────────────────────────
    loop_ms: int = 50               # position poll period
    # move_to wrong-way trip: consecutive ticks the target error may GROW
    # (by more than the tolerance band) before the move is aborted —
    # protects the first live runs against a wrong fwd_increases_counts.
    wrongway_ticks: int = 6
    # stall warning: consecutive ticks an axis may be commanded with
    # FROZEN counts before the "module is not stepping" warning
    # (~1 s at the 50 ms loop)
    stall_ticks: int = 20
    # …and the point where a stalled command is ABORTED outright (a
    # faulted module must not stay commanded — it would lurch the moment
    # the fault clears). ~3 s at the 50 ms loop.
    stall_abort_ticks: int = 60

    # ── motion shaping (the host-side accel/velocity protection) ────────
    # The PLC fixes the stepper speed (±2000 steps/s) and accel in its
    # program — there is no Modbus register for either. What the host
    # CAN do is never slam the modules: any stop→start or direction
    # reversal passes through a commanded-stop dwell longer than the
    # PLC's own 250 ms stop/disable sequence, so conflicting commands
    # (the likely start/stop fault source) can't reach a module
    # mid-sequence. Stops themselves are NEVER delayed.
    direction_dwell_ms: int = 600
    # a move that overshoots and re-reverses this many times is aborted
    # (tolerance too tight for the fixed PLC speed)
    max_reversals: int = 3

    # read the 750-673 status bytes from the input image each tick
    read_module_status: bool = True

    # ── host-side homing (StatusWord limit bit + jog) ───────────────────
    # Limit-bit polarity (rig-verified 2026-07-22): the StatusWord bit
    # is CLEAR (0) when a switch is ENGAGED and SET (1) when healthy —
    # the NC chain drives the input high in the normal state. True =
    # that reversed sense; False = bit set means engaged (the original
    # assumption, kept only as an escape hatch).
    limit_active_low: bool = True

    # After the switch RELEASES during backoff, keep jogging away for
    # this long before stopping — the PLC speed is fixed (no host "slow"
    # jog), so this margin time bounds the overshoot past the switch
    # release point (~2000 steps/s × margin).
    home_backoff_margin_s: float = 0.25
    # phase deadlines; on breach the axis is stopped and the homing
    # cycle faults cleanly (homed stays False)
    home_seek_timeout_s: float = 120.0
    home_backoff_timeout_s: float = 20.0

    # counter-jump guard: a per-tick raw delta above this is a COUNTER
    # event (module reset / power event), not motion — the position is
    # held and re-based instead of integrating a phantom move. Fastest
    # observed axis: Z ≈ 39k counts/s ≈ 2k/tick; 100k is 50× margin
    # (a physical move can never cross ~half the 1M ring in one tick).
    max_counts_per_tick: int = 100_000

    # ── modbus ───────────────────────────────────────────────────────────
    modbus_timeout_s: float = 1.0
    max_consecutive_errors: int = 5  # watchdog: stop all + drop on breach

    # ── behaviour / display ──────────────────────────────────────────────
    force_sim: bool = False
    plot_window_s: float = 120.0

    def axes(self) -> List[AxisConfig]:
        return [self.x, self.y, self.z]

    def axis(self, name: str) -> AxisConfig:
        try:
            return {"x": self.x, "y": self.y, "z": self.z}[name.lower()]
        except KeyError:
            raise ValueError(f"unknown axis {name!r}") from None

    # ── serialization ────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "TraverseConfig":
        """Build from a dict, IGNORING unknown keys — old saved JSONs may
        still carry retired fields (speed_steps_s, homing_supported,
        home_value, …); they are dropped silently for backward compat."""
        d = dict(d)
        known = {f for f in cls.__dataclass_fields__}      # noqa: E1101
        ax_fields = {f for f in AxisConfig.__dataclass_fields__}  # noqa: E1101

        def mk_axis(a: Optional[dict], default) -> AxisConfig:
            if not a:
                return default()
            return AxisConfig(**{k: v for k, v in a.items()
                                 if k in ax_fields})

        cfg = cls(x=mk_axis(d.pop("x", None), _x),
                  y=mk_axis(d.pop("y", None), _y),
                  z=mk_axis(d.pop("z", None), _z),
                  **{k: v for k, v in d.items() if k in known and
                     k not in ("x", "y", "z")})
        return cfg

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path) -> "TraverseConfig":
        return cls.from_dict(json.loads(Path(path).read_text(
            encoding="utf-8")))


def load_startup_config() -> "TraverseConfig":
    """The app-launch auto-load: ``defaults_path()`` if present.

    Guarded — an unreadable/corrupt defaults file logs a warning and
    falls back to factory defaults instead of blocking the launch.
    """
    import logging
    p = defaults_path()
    if p.exists():
        try:
            cfg = TraverseConfig.load(p)
            logging.getLogger(__name__).info("defaults loaded from %s", p)
            return cfg
        except Exception as exc:                       # noqa: BLE001
            logging.getLogger(__name__).warning(
                "defaults file %s unreadable (%s) — factory defaults",
                p, exc)
    return TraverseConfig()


def _u32_to_signed(v: int) -> int:
    return v - 0x1_0000_0000 if v >= 0x8000_0000 else v


def slopes_from_legacy_xml(path) -> dict:
    """Signed clicks/inch per axis from a C# sswtTraverseCalibrationFile.xml.

    The legacy file stores encoder readings as uint32 and the slope as a
    magnitude; the signed slope is re-derived from the high/low point
    pairs. Returns {"X": slope, "Y": slope, "Z": slope}; offsets are NOT
    returned (the 750-673 counter zeroes at power-up, so the file's
    offsets are stale — re-home / re-zero on the rig instead).
    """
    import xml.etree.ElementTree as ET
    root = ET.parse(str(path)).getroot()

    def val(tag: str) -> float:
        el = root.find(tag)
        if el is None or el.text is None:
            raise ValueError(f"{path}: missing <{tag}>")
        return float(el.text)

    out = {}
    for ax in ("X", "Y", "Z"):
        inch_hi, inch_lo = val(f"Axis_{ax}_High"), val(f"Axis_{ax}_Low")
        c_hi = _u32_to_signed(int(val(f"EncoderReading_{ax}_High")))
        c_lo = _u32_to_signed(int(val(f"EncoderReading_{ax}_Low")))
        if abs(inch_hi - inch_lo) < 1e-9:
            raise ValueError(f"{path}: axis {ax} cal points coincide")
        out[ax] = (c_hi - c_lo) / (inch_hi - inch_lo)
    return out
