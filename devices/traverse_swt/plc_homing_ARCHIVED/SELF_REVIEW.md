# Adversarial self-review — FB_AxisHoming and integration code

Line-by-line pass of `FB_AxisHoming.st.txt`, `GlobalDecls.st.txt` and
`CallSites.st.txt` against: the mailbox rules, the rising-edge rules, the
existing jog-pattern timings, NC limit polarity, the rotary seed ordering,
and the safety rules. Section 1 lists defects **found and fixed** during
the pass; Section 2 lists deliberate design points that were challenged
and kept (with the reasoning); Section 3 is the numbered VERIFY-ON-RIG
list.

---

## 1. Issues found AND fixed

1. **Stale `wPend` / `iNext` across runs.** The stop-sequence watchdog
   (`tonGuard`) reports `wPend` if nonzero. After a faulted run, a later
   run that tripped the watchdog would have re-reported the *previous*
   run's fault code. Fixed: `wPend := FC_NONE; iNext := ST_IDLE;` cleared
   in the start-acceptance block.

2. **Stale `xSetPosStart` kills the SetPosition rising edge.** The
   PRESET timeout-while-Busy branch deliberately leaves `xSetPosStart`
   TRUE (never abort a mailbox command mid-Busy, constraint [S2b]). But a
   subsequent re-home would then set it TRUE-on-TRUE in PRESET phase 0 —
   no rising edge, command never issued, guaranteed 30 s timeout. Fixed
   twice over: (a) `xSetPosStart := FALSE` at start acceptance (safe: a
   hung mailbox must be reset before re-homing anyway, and SEEK/BACKOFF
   provide many scans of FALSE before the next edge); (b) PRESET phase 0
   now refuses to re-edge and faults (code 5) if `SetPos.xBusy` is still
   TRUE at entry.

3. **No parameter range validation.** `diHomeValue` outside the module's
   position range −8388607…+8388607 ([S1] l.46) would make SetPosition
   fail mid-sequence; a rotary `diHomeValue` ≥ one period would make the
   wrap seed (wrap = 0, offset = 0) silently wrong — a *datum corruption*
   bug, the worst kind. Fixed: start acceptance validates both values and
   refuses with FAULT 5 (bad home value) / FAULT 6 (bad park target)
   before any motion.

4. **Misleading comment** in ST_MOVE phase 1 described the MoveABS uptake
   window as "100ms*5" while `tonMbx` PT is 100 ms. Fixed to state
   100 ms.

5. **Abort racing a start in the same scan.** Verified rather than fixed:
   the IDLE/DONE/FAULT branch runs the start block first and the
   abort-pending block second, so a simultaneous START + ABORT ends in
   FAULT 7 with all Motor command fields dropped. Abort wins. Correct.

## 2. Challenged and kept (deliberate deviations, with reasons)

1. **`Motor.BasicStart` is explicitly cleared in the shared stop sequence
   (phase 90), which the original SFC actions do not do.** The SFC gets
   its rising edges from step re-entry; a single FB re-writing the same
   fields cannot, and the rig rule is that all MC3 execute inputs are
   rising-edge triggered ([S2b]). Clearing in the stop phase guarantees
   every SEEK/BACKOFF/MOVE command presents a fresh FALSE→TRUE.

2. **Stop-gate timing standardized on the Axial pattern: 100 ms stop
   gate + `BasicDone`, 250 ms disable dwell** ([S1] l.347/348/354, and
   the generic `StepMotion` FB at l.467/472 uses the same 100/250 pair).
   Note the Lateral/Vertical SFC actions use 250 ms for the *stop* timer
   too ([S1] l.377/407); 100 ms is the specified and proven-on-Axial
   value, and the gate is ANDed with `BasicDone` so it cannot release
   early. Kept at 100/250 for all axes.

3. **Mailbox arbitration around PRESET** — the required sequence is
   enforced structurally, not by flags: the only path into ST_PRESET is
   through the shared stop sequence (drops `Velocity`, `MoveABS`,
   `BasicEnable` → Basic block releases the mailbox) plus ST_SETTLE,
   which requires `BasicMailboxActive` FALSE for a continuous 100 ms on
   top of the 200 ms settle before it will hand over. No motion command
   exists in ST_PRESET. MOVE_OFFSET then re-enables the Basic block and
   uses its own `MoveABS` request rather than a standalone
   `MC3_MoveAbsolute` instance, so the mailbox has exactly one owner at
   every instant ([S2b]).

4. **`ControlWord := 0` does not abort homing.** Jog bits are masked
   while homing is busy (homing wins); the commanded kill for homing is
   HomeCmd bit1 (or the physical E-stop). This is deliberate — a host
   that zeroes ControlWord as routine housekeeping must not silently
   abort a homing cycle — and is called out in COMMISSIONING.md §0 as an
   operator briefing item.

5. **Abort during MOVE_OFFSET leaves `xHomed` TRUE alongside FAULT 7.**
   The datum was validly established at PRESET; only the park move was
   abandoned. HomeStatus shows bit8 + bit10 together; documented in the
   map. Kept.

6. **One-scan latency on all TON `.Q` evaluations** (timers are called
   once at the bottom of the body; phases read `.Q` the next scan). At
   the 100 ms…120 s time scales involved, one PLC scan is noise; in
   exchange every TON has exactly one call site — the classic 2.3 pattern
   that prevents double-call timer corruption.

7. **A limit engaged at start always faults (code 4), even if it happens
   to be the target switch.** The 750-673 does not report limit
   *direction* in the MC3 status bits ("only 750-670, 750-671", [S1]
   l.164-165), so the PLC cannot prove which switch it is on; guessing
   wrong would seek *away* through full travel into the far switch.
   Manual jog-off first is the only safe policy.

8. **FAULT 3 (BOTH_LIMITS) ships disarmed** (`BOTH_LIMIT_MASK = 0`).
   With one series-NC limit input per axis and no direction status, both
   switches are indistinguishable on the I_* bit; the only both-limits
   evidence is in the module status byte whose bit layout is a manual
   fact we refused to fabricate. The check exists, is fully wired, and is
   armed by setting one constant after the commissioning measurement
   (COMMISSIONING.md §3.2).

9. **Native MC3_HOME recommendation** (constraint [S2f]): the library's
   `MC3_HOME` / `Motor.Homing` request path ([S1] l.24-26, 233) with
   `BasicRefPosition` is documented as the *preferred future path for the
   linear X and Y axes* once `Reference_Mode` is configured — it is the
   manufacturer's integrated route. It must never be used on the rotary
   axis: a module-internal reference wipes the modulo counter without the
   PLC-side wrap counter/offset being reseeded in the same scan, which is
   exactly the corruption this FB's PRESET/xDatumPulse choreography
   prevents. Hence the explicit jog-based FB is the required and only
   implementation for Z, and the uniform implementation for all three
   axes today.

10. **Residual edge case, accepted:** if `BasicMailboxActive` toggles
    periodically while the Basic block is disabled (it should not), the
    SETTLE 10 s supervision restarts on each phase-1→0 bounce and the
    state could in principle dwell indefinitely at zero motion. ABORT
    remains available from that state at all times; not worth extra
    complexity.

## 3. Safety-rule cross-check (all pass)

- Seek speed: default 300, clamped 50…1000 in the FB regardless of host
  writes; backoff 150, clamped 25…500. SetupSpeed 3000 untouched.
- Both-limits → FAULT, no auto-recovery (once mask armed); engaged limit
  at start → FAULT 4, no auto-recovery, no motion.
- Abort reachable from every state, including motionless ones; always
  ends: commanded stop (where moving), `Velocity`/`MoveABS`/`BasicEnable`
  /`BasicStart` all FALSE, FAULT 7, drive stopped + disabled.
- Zero motion commands exist in SETTLE, PRESET, DONE, FAULT, IDLE.
- MOVE_OFFSET is unreachable except through `SetPos.xDone` (the Done
  gate) — checked: the only assignment `iState := ST_MOVE` is inside the
  `IF SetPos.xDone` branch.
- Homing never auto-starts: the only sequence entry is `rtStart.Q`
  (R_TRIG on the gated host START), and the call site additionally gates
  on ≥ 1 s jog quiet.
- NC polarity handled in exactly one place (`xLimitEngaged :=
  NOT xLimitInput`), so a rig-discovered inversion is a one-line fix.
- Rotary seed: `diWrapCount := 0`, `diDatumOffset := 0`, `xDatumPulse`,
  `xHomedRetain := TRUE` all in the same scan, inside the `SetPos.xDone`
  branch — the scan the preset lands ([S2e]).

## 4. VERIFY ON RIG — consolidated numbered list

1. `MC3_SetPosition` formal pin names (`xStart`, `diPosition`, `Stepper`,
   and the Done/Busy/Error outputs). Open `Stepper_03.lib` in the Library
   Manager; adjust the three marked call lines and the three reads in
   ST_PRESET. ([S1] gives only instance-side names: SetPos_Done/_Busy/
   _Err.)
2. `Motor.BasicStepper` is the `MC3_typData` communication struct to wire
   into mailbox blocks ([S1] l.231/9/12 strongly imply it; confirm in the
   Library Manager type view).
3. `I_*Limit` polarity at the PLC variable: expect TRUE untouched, FALSE
   pressed (NC). Press each switch by hand before first motion
   (COMMISSIONING §3.1).
4. Status1 byte (`MotorIn[11]`) limit bit positions; then arm
   `BOTH_LIMIT_MASK`, and confirm the data type of `Motor.BasicStatus`
   (WORD assumed in the mask compare — adjust the two conversion calls if
   it is BYTE).
5. `BasicRefPosition` is the MoveABS target register when the
   `Motor.MoveABS` request is used with `MC3_StepperControlBasic`.
6. `BasicSpeed` sign/magnitude semantics in MoveABS mode (jog mode is
   signed, [S1] l.336/338; positioning may want magnitude only).
7. `BasicDone` behavior on a zero-length MoveABS (the 100 ms uptake
   window in ST_MOVE phase 1 assumes Done-without-Busy means "already at
   target").
8. Recovery procedure for a SetPosition stuck Busy (FAULT 5 with the
   command left standing): MC3_RESET vs. power cycle.
9. Retain memory actually configured on the 750 coupler target (NOVRAM):
   home an axis, power-cycle, confirm HomeStatus bit8 and (Z) the wrap
   retains survive (INTEGRATION §8, COMMISSIONING §7.3).
10. The jog SFC's initial step writes nothing to `Motor.*` fields, so the
    1 s jog-quiet gate is a sufficient hand-over guarantee.
11. The existing PLC-side wrap-reconstruction code for Z: locate its wrap
    counter / last-modulo variables; wire `xDatumPulse` to re-seed the
    last-modulo memory, and either substitute the existing retained wrap
    counter for `diVerticalWrapR` or delete the duplicate — exactly one
    wrap counter may exist.
12. Confirm Z (Vertical) is indeed the rotary big-stage axis and that
    `Rotary_Axis_Period` in `ConfigurationData_673` equals 8,000,000 as
    wired in CallSites Block B3.
13. HomeCmd bit2 direction convention vs. physical wiring: with bit2 = 1,
    SEEK must move toward the switch the crew calls the "positive" end on
    each axis (first verified at commissioning §4, SEEK row).
