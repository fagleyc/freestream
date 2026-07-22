# Homing Modbus Map — WAGO 750 coupler @ 192.168.1.21

Per-axis block of 10 holding registers, fixed layout. `%MW0–%MW21` are
already taken by the existing map (0/1 control/status, 10–15 positions,
16–21 speed/accel); the homing blocks start at `%MW22`.

Wire-address rule (WAGO 750): `%MW0` = Modbus holding register **12288**
(0x3000), so register = 12288 + MW number.

| Axis | Motion | Block | %MW range | Modbus (FC3/FC16) |
|------|--------------|-------|-----------|-------------------|
| X | Axial | X | %MW22–31 | **12310–12319** |
| Y | Lateral | Y | %MW32–41 | **12320–12329** |
| Z | Vertical (rotary big stage) | Z | %MW42–51 | **12330–12339** |

## Block layout (offsets from block base)

| Off | Name | Type | Dir | Meaning |
|-----|------|------|-----|---------|
| +0 | HomeCmd | WORD | host→PLC | bit0 **START** (host sets, PLC clears on acceptance); bit1 **ABORT** (one-shot, PLC clears when seen); bit2 **HOME_TO_POSITIVE_LIMIT** (0 = home to negative limit) |
| +1 | HomeStatus | WORD | PLC→host | low byte = state enum: 0 IDLE, 1 SEEK, 2 BACKOFF, 3 SETTLE, 4 PRESET, 5 MOVE_OFFSET, 6 DONE, 7 FAULT; bit8 **xHomed** (RETAIN-backed, survives power cycle); bit9 **xBusy**; bit10 **xFault** |
| +2 | wFaultCode | WORD | PLC→host | 1 SEEK_TIMEOUT (no limit found), 2 BACKOFF_TIMEOUT (switch stuck), 3 BOTH_LIMITS, 4 WRONG_LIMIT_AT_START (a limit was already engaged — jog off manually, then retry), 5 PRESET_FAIL, 6 MOVE_FAIL, 7 ABORTED, 8 MODULE_ERROR |
| +3/+4 | diHomeValue | DINT | host→PLC | counter preset at datum. **Low word first** (offset +3 = low), same convention as the existing `AxialPosition AT %MW10 : DINT` |
| +5/+6 | diParkPosition | DINT | host→PLC | post-home MoveAbsolute target, low word first. Set equal to diHomeValue to skip the offset move |
| +7 | iSeekSpeed | INT | host→PLC | default 300 if 0; PLC clamps to 50…1000 regardless of what is written (SetupSpeed is 3000 — homing must stay well below) |
| +8 | iBackoffSpeed | INT | host→PLC | default 150 if 0; PLC clamps to 25…500 |
| +9 | reserved | WORD | — | write 0 |

## Protocol

1. Host writes +3…+8 (params) **before** setting START. The FB latches the
   parameters at acceptance; mid-run writes are ignored until the next home.
2. Host sets HomeCmd bit0. PLC clears bit0 in the scan the request is
   accepted. If a ControlWord jog bit for that axis is set, acceptance is
   deferred until the jogs have been released for ≥ 1 s (the bit stays set,
   visibly pending; the host may clear bit0 itself to withdraw).
3. Host polls HomeStatus and watches the enum walk 1→2→3→4→(5)→6.
   bit9 (busy) drops at DONE or FAULT.
4. ABORT: host sets bit1 at any time; PLC clears it immediately and the FB
   performs a commanded stop, then reports FAULT with code 7 (ABORTED).
   ControlWord jog bits are ignored while homing is busy.

## Read the whole homing block in one FC3

All three axes are contiguous: **FC3, start 12310, count 30** returns
`%MW22…%MW51` (X, Y, Z blocks back-to-back) in a single transaction.
Recommended for the Python driver's status poll. Writes of the parameter
words can likewise go out as one FC16 per axis (start 12313, count 6 for X).
