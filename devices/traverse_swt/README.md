# traverse_swt — SSWT 3-axis traverse (WAGO 750 PLC)

Standalone AeroVIS device driver + PyQt6 dark-mode GUI for the sub-sonic
wind tunnel traverse: a WAGO 750 controller at **192.168.1.21:502**
(Modbus TCP, unit 1) driving three **750-673** stepper modules —
X axial, Y lateral, Z vertical.

```
python run_traverse_app.py --sim     # plant sim
python run_traverse_app.py          # real PLC
python probe_traverse.py            # READ-ONLY live probe
```

> **Removed 2026-07 (non-functional on the rig):** software jog and the
> stepper-reset pulse — neither worked on the hardware. The limit
> switches were later **fixed** (inverted at the module) and host-side
> limit reaction + host-side homing are back — see "Limit switches"
> and "Homing" below. The speed/accel-over-Modbus register spec and
> the PLC-side homing POUs (`plc_homing_ARCHIVED/`) were retired
> without ever being downloaded.

## Where this came from

Reverse-engineered 2026-07-07 from two sources:

* **PLC source** — `Tunnel Control\SWT_Controls\WAGO\LVW_V3_2021.pro`
  (CoDeSys 2.3; ST source extracted from the .pro binary)
* **Deployed C#** — `Tunnel Control\TunnelVision\Tool_SSWT_Traverse\`
  (`FTool_SSWT_Traverse.cs`, FieldTalk Modbus), defaults from
  `Core\XConfiguration\XController.cs` (`XControllerTraverseSSWT`,
  IP 192.168.1.21:502, slave 1, move tolerance 0.02")

## Register map (wire = 0-based, what pymodbus uses)

The C# FieldTalk library counts registers from **1**, so every C#
address is +1 vs the wire — same as the AC Delta finding.

| wire  | %MW  | C# addr | meaning                                        |
|-------|------|---------|------------------------------------------------|
| 12288 | MW0  | 12289   | **ControlWord** — host writes (FC6)            |
| 12289 | MW1  | 12290*  | **StatusWord** — limit bits 0/1/2 = X/Y/Z      |
| 12298 | MW10 | 12299   | X position, DINT **low-word-first** (FC3/FC4)  |
| 12300 | MW12 | 12301   | Y position, DINT                               |
| 12302 | MW14 | 12303   | Z position, DINT                               |

\* the C# declared 12290 as a "home button" register but **never used
it** — and it lands on the StatusWord anyway. There is no homing in the
PLC (`Homing := FALSE` everywhere); homing is host-side (below). The
StatusWord's limit bits are **live again** (2026-07): bit0 = X/Axial,
bit1 = Y/Lateral, bit2 = Z/Vertical — the NEGATIVE-direction switch of
each axis, fixed (inverted) at the module.

**ControlWord bits** (PLC source: `ControlWord AT %MW0`):

| bit | PLC name        | bit | PLC name        | bit | PLC name       |
|-----|-----------------|-----|-----------------|-----|----------------|
| 0   | AxialJogFwd     | 2   | LateralJogFwd   | 4   | VerticalJogFwd |
| 1   | AxialJogRev     | 3   | LateralJogRev   | 5   | VerticalJogRev |

(The "Jog" names are the PLC's — the host uses these direction bits for
bang-bang `move_to`, not a software jog. Bits 6/7/8 were the per-axis
stepper BasicReset; the reset pulse was removed as non-functional.)

⚠ The C# labeled **Y and Z with fwd/rev swapped** vs the PLC names
(C# "Y forward" = 8 = bit3 = PLC LateralJog**Rev**; C# "Z forward" = 32
= bit5 = PLC VerticalJog**Rev**). This driver's default masks reproduce
the **C# behavior** (what operators knew); each axis has a
`fwd_increases_counts` flag to flip the sense, and `move_to` has a
wrong-way trip that stops the axis if the error grows.

**Motion model**: the PLC runs the steppers in velocity mode at a fixed
±2000 steps/s (accel/decel baked in). The host has direction bits only —
no speed, no position mode. Positioning is a host-side bang-bang loop
(command direction → watch counts → drop the bit inside the tolerance
band, default 0.02").

**DINT word order**: CoDeSys stores the low word at the lower %MW; the
C#'s FieldTalk `readInputLongInts` default is also low-word-first and it
produced sane calibration values, so `plc._dint` is low-first.
`probe_traverse.py` verifies this live (the high word of a realistic
position is 0x0000/0xFFFF).

## Limit switches — live again, HOST-side reaction only

The rig's limit switches were dead for years (wired inverted); they
were **fixed at the module (2026-07)** and now function. The PLC
copies them into **StatusWord %MW1**: bit0 = X/Axial, bit1 =
Y/Lateral, bit2 = Z/Vertical — each the axis's **negative-direction**
switch.

**Polarity (rig-verified 2026-07-22): the bits are ACTIVE-LOW.** The
NC chain drives each bit HIGH in the healthy state; the bit **CLEARS
when the switch is engaged** (`limit_active_low = True`, the default —
an escape hatch in Settings ▸ Advanced flips it back if the PLC wiring
ever changes). **X/Axial's limit input is DISABLED**
(`x.limit_enabled = False`, per the rig): its bit is ignored entirely
— no runtime reaction and no homing on X.

Crucially, the module hardware-limit lockout has been **UNLINKED**
(`Ptr_LimitSwitch = 0`): the 750-673 never hard-stops or latches on a
limit by itself. **The host is the only protection.** The driver reads
the StatusWord in every control tick (it is part of the 16-register
block read) and:

* **Runtime limit trip** — if an axis's switch ENGAGES (bit clears)
  while the axis is commanded TOWARD the negative limit (outside a
  homing sequence), the host stops the axis within one control tick
  and flags a `LIMIT` fault (shown on the axis card's state label and
  in the status log). Commanding AWAY from a made switch is allowed —
  that is the recovery path — and the fault clears with the switch.
* **Homing** uses the same engaged-sense deliberately — see below.

Travel is otherwise protected by the calibrated **soft limits**
(`min_in`/`max_in`) and the generic stall/wrong-way guards.

(Speed/accel are FIXED in the PLC program at ~2000 steps/s — the
speed/acceleration-over-Modbus register spec this README used to carry
was retired without ever being downloaded; the host has direction bits
only.)

## Homing — host-side sequence (per axis, Y/Z only)

There is no homing in the PLC. `TraverseDrive.home_axis(axis)` runs
the whole cycle host-side, reusing the ordinary jog command path,
position tracking and the StatusWord limit bit (no extra PLC
registers; the PLC-side homing scaffolding in `plc_homing_ARCHIVED/`
was never downloaded):

1. **SEEK** — jog toward the NEGATIVE limit (the ControlWord bit that
   decreases inches, via the axis's `fwd_increases_counts` sense —
   *verify the direction live on the first supervised run*), polling
   the axis's StatusWord limit bit every control tick. The loop
   tightens from the normal 50 ms (~20 Hz) to ~15 ms (~66 Hz) while a
   homing cycle runs, so the reaction bound is ≈2000 steps/s × 15 ms ≈
   30 counts of overtravel.
2. **Bit sets → stop** (the jog is dropped in that same tick's
   ControlWord write).
3. **BACKOFF** — jog the OPPOSITE direction until the bit clears,
   **plus** `home_backoff_margin_s` (default 0.25 s). The PLC speed is
   fixed — there is no host-controllable "slow" jog — so the margin
   *time* bounds the overshoot past the switch release point.
4. **Stop, settle**, read the current unwrapped counts and call
   `calibrate_offset(home_datum_in, counts)` — the limit position
   reads the datum (to within the backoff-margin travel). The homed
   flag sets; `is_homed(axis)` reports it.

Per-axis config: `home_enabled` (X **False** — `home_axis("x")` raises
`ValueError("no homing on X …")`; Y/Z True) and `home_datum_in`
(default **−18.0"** for Y and Z, their negative switches). Seek and
backoff have their own timeouts (`home_seek_timeout_s` 120 s /
`home_backoff_timeout_s` 20 s) that fault cleanly: axis stopped, homed
stays False, a `HomingResult` with the fault reason. Per-axis STOP,
"Stop all" and E-STOP abort a homing cycle at any point and leave the
axis stopped.

**Per-power-cycle:** the 750-673 position counter zeroes at module
power-up, so the homing offset dies with it — **re-home each setup**.
The offset persists only if you save the config ("Set as Defaults" /
Save config), same as any other calibration.

**TODO(Casey): real travel ranges.** The actual travel of Y and Z from
the −18.0" datum is UNKNOWN. The defaults set `min_in = −18.0` and
keep the old spans as placeholders only (Y −18…−8", Z −18…−15.94") —
measure the real ranges from the datum on the rig and set them in
Settings.

## Position counter: clean 1,000,000-count rollover + absolute tracking

The 750-673 modules are **configured to roll their position counter
over cleanly at 1,000,000 counts** (unsigned **0…999,999**, wrapping
999999→0 going up and 0→999999 going down) — consistent for all three
axes. This was done deliberately so the module never approaches the
24-bit counter limit where it used to STOP stepping; that failure mode
(and the MC3_SetPosition re-reference workaround this README used to
describe) is gone.

The driver **unwraps the 1M ring into a continuous ABSOLUTE position**
(`wrap_modulus` per axis, default 1,000,000; 0 disables): each 50 ms
tick the shortest-path modular delta
(`(raw − raw_prev + m/2) mod m − m/2`) is accumulated into
`counts`, which is unbounded and can exceed ±1M. Calibration
(inches↔counts), soft limits, move targets and the bang-bang loop all
operate on this absolute position, so a single move can cross any
number of wrap boundaries — targets millions of counts away converge.

A **counter-jump guard** (`max_counts_per_tick`, default 100k) still
protects the unwrap: a physical move can never cross ~half the ring in
one tick, so a bigger raw delta is a COUNTER event (module reset /
power event) — the position is held and the raw baseline re-based
instead of integrating a phantom move.

> Historical note: the modules previously ran a signed 24-bit counter
> that saturated at ±2^23 (the module silently stopped stepping there,
> observed live 2026-07-07). The 1M rollover configuration superseded
> the whole counter-limit / MC3_SetPosition re-reference machinery
> (overflow warnings, Re-ref 0 button, %MW2–%MW9 setpos registers).

## Calibration

C# two-point form, per axis, **signed** slope:
`inches = inch_high − (counts_high − counts) / clicks_per_inch`.

Signed slopes derived from the rig's `sswtTraverseCalibrationFile.xml`
(shipped as defaults): X **+13705.6**, Y **−14841.0**, Z **−986938.4**
clicks/inch (Y and Z counts run backwards vs inches). Cal-file travel:
X ±5", Y ±18", Z ±18" (soft-limit defaults; Y/Z set from the rig
2026-07-22, homing datum −18" at the negative switch).

The 750-673 position counter **zeroes at PLC power-up**, so the offset
is per-power-cycle: axes start `calibrated=False`. **Homing Y/Z sets
their offsets** (the homing sequence ends in `calibrate_offset` at the
datum); X — or any axis — can also be re-zeroed against a known
position (routine 2 in the Calibration tab). *Import legacy C# cal
XML…* pulls signed slopes from the old file.

## Architecture

Same layering as `ac_delta` (lifecycle `connect/start/stop/disconnect`,
`on_status` callback, device-owned ring buffer):

* `plc.py` — `WagoTraversePlc`: ONE persistent connection (the C#
  reconnected before *every* operation), one 16-register block read per
  tick, change-only ControlWord writes (stops always forced through).
  Also reads the physical input image @ addr 0 (18 words) for the
  **750-673 module status bytes** S1·S2·S3 per axis (input bytes
  11/10/9 per the CoDeSys stepper lib) — shown live on each axis card
  and in Diagnostics, with every S1 transition logged (time, position,
  direction). Bit meanings are undocumented in the extracted source;
  one faulting start on the rig identifies the error bit empirically.
  Degrades gracefully if the gateway rejects the read.

**Motion shaping (start/stop fault protection).** The stock PLC fixes
the stepper speed (±2000 steps/s) and accel in its program — the host
has no speed control. What the host enforces:

* **Direction-change dwell** (default 600 ms, Settings): any start or
  reversal passes through a commanded stop longer than the PLC's own
  250 ms stop/disable sequence, so a module is never hit with a
  conflicting command mid-sequence — the likely cause of the
  start/stop faults. Stops themselves are never delayed.
* **Stall abort**: a commanded axis whose counts stay frozen warns at
  ~1 s and is ABORTED at ~3 s (a faulted module must not stay
  commanded — it would lurch when the fault clears).
* **Oscillation guard**: a move that re-reverses more than
  `max_reversals` times around the target aborts with a
  tolerance-too-tight message instead of ping-ponging the motor.
* `device.py` — `TraverseDrive`: single control thread for all three
  axes (multi-axis `move_to` starts the same tick), soft limits,
  host-side limit reaction + homing (StatusWord bits), wrong-way trip,
  stall detection, Modbus watchdog, E-stop from the calling thread.
* `emulator.py` — `SimPlc` plant sim (velocity integrator on the
  unsigned 1M ring at the fixed ~2000 counts/s PLC speed, per-axis
  negative-end limit switches that assert the StatusWord bits, and an
  injectable `stalled_axes` fault hook) — homing runs end-to-end in
  sim.
* `app/` — Motion (axis cards + per-axis STOP + E-STOP) / Diagnostics /
  Calibration tabs.

## First live checklist (supervised) — status 2026-07-07

1. `python probe_traverse.py` — verify map, word order, FC3=FC4 before
   any motion.
2. GUI, live, one axis: move the stage a known amount from the console
   and confirm counts track it (and the direction). If counts fall as
   inches rise, that is the signed slope — captured by calibration.
3. Re-zero calibration, then a short `move_to` — the wrong-way trip
   guards the first one.

**Live findings so far:**

* **Counter model (updated 2026-07):** the modules are reconfigured to
  a clean unsigned **1,000,000-count rollover** on all three axes —
  see "Position counter" above. The original live findings (Z's signed
  24-bit wrap, and the module silently stopping at its ±2^23 counter
  limit after a −20" Z move) drove that reconfiguration; the old
  counter-limit warnings and MC3_SetPosition re-reference support were
  removed once the rollover made them moot.
* **S1 status codes observed live:** 0x00 idle, 0x03 starting, 0x1B
  running, 0x01 stopping, 0x1F transient at start. No distinct code at
  the counter-limit stop.
* **Crash fix:** a raw pymodbus `ModbusIOException` (gateway timeout)
  escaped `PlcError` handling and killed the control thread live. All
  transport exceptions are now wrapped, and the loop has a catch-all
  that emergency-stops + disconnects instead of dying (same hardening
  applied to tunnel_plc and ac_delta).

* **Z direction VERIFIED (the hard way):** `move_to(z=+1")` wrong-way-
  tripped twice — commanding Z's rev bit (bit4) *increases* the counts,
  so the C#-"forward" bit decreases them. Fixed:
  `z.fwd_increases_counts = False` is now the default.
* **Y (lateral) does not move the drive.** Commands go out but nothing
  happens. The driver raises a STALL warning (and then aborts) when a
  commanded axis's counts stay frozen (~1 s). Diagnosis order:
  1. Diagnostics tab → ControlWord echo: bits 2/3 must appear while a Y
     `move_to` is running (confirms the PLC received the command).
  2. Watch Y counts. **Frozen counts** = the 750-673 module is not
     issuing steps (the counter is the module's own step count) → the
     module is faulted/disabled; check the module's error LEDs in the
     cabinet.
  3. **Counts moving but no physical motion** = motor power/wiring
     (stepper is open loop — the module can count steps into a dead
     motor circuit).
  4. If it does not clear, the lateral drive may need a PLC power cycle
     (the CoDeSys program also has MC3_RestoreDefault plumbing for the
     lateral module, suggesting past trouble with it).
