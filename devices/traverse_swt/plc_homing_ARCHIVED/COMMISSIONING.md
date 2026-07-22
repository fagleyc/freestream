# Homing Commissioning Checklist — 3-axis WAGO traverse
### First power-on with the new homing code. One axis at a time, hand on the E-stop.

## 0. Safety ground rules (read first)

- **Three independent ways to kill motion — verify all three before the
  first SEEK:**
  1. **Physical E-stop** — drops drive power. Always primary.
  2. **HomeCmd ABORT** (bit1 of %MW22/32/42) — commanded stop from the
     host; FB decelerates, disables the drive, latches FAULT 7.
  3. **ControlWord := 0** — kills any *jog*; note it does **not** stop an
     in-progress homing (homing deliberately ignores jog bits while busy)
     — that is what ABORT is for. Brief the operator on this difference.
- **Homing NEVER auto-starts.** Not at power-up, not at boot-project
  start, not on fault reset. Only a fresh rising edge of the START bit
  begins a sequence. Confirm at first power-on: HomeStatus low byte must
  read 0 (IDLE) and stay there.
- Person at the E-stop must have sight of the axis under test for the
  entire sequence.
- Clear the tunnel section / probe volume before any homing run.

## 1. Test order

| # | Axis | Why this order |
|---|------|----------------|
| 1 | **Y — Lateral** | shortest, safest travel; proves the whole state machine cheaply |
| 2 | **X — Axial** | linear, longer travel |
| 3 | **Z — Vertical, LAST** | the rotary big stage (~986,938 counts/inch, 44 in travel, period 8,000,000): adds the wrap-seed logic on top of an already-proven sequence — never debug both at once |

## 2. Speeds

- Leave iSeekSpeed = **300** (default) and iBackoffSpeed = **150** for all
  commissioning. SetupSpeed on this rig is **3000**; homing must run well
  below it because SEEK ends by *striking a hardware switch* — kinetic
  energy and overtravel past the trip point scale with speed, and the
  datum repeatability of a bump-and-back-off cycle degrades at high
  approach speeds. 300 is 10 % of setup speed.
- The FB clamps to 50…1000 (seek) and 25…500 (backoff) no matter what the
  host writes — but do not rely on the clamp: write sane values.
- First-ever run per axis: consider seek 100 to watch everything in slow
  motion, then repeat at 300.

## 3. Pre-run checks (per axis, drive power ON, motion not yet commanded)

1. **Limit input polarity (NC):** watch the axis `I_*Limit` bit
   (StatusWord bits 0–2, or the watch list). Expect **TRUE (1) with the
   switch untouched** (NC circuit closed) and **FALSE (0) when you press
   the switch by hand**. If inverted, STOP — fix the FB's limit sense
   before any motion (operator-verified constraint c; VERIFY ON RIG item).
2. **Module status bytes:** note the idle value of `MotorIn[11]`
   (Status1), `[10]` (Status2), `[9]` (Status3). Press each limit switch
   and record which bits change — this is the data needed to arm the
   BOTH_LIMIT_MASK both-limits check (currently disabled = 0).
3. **Mailbox idle:** `?Motor.BasicMailboxActive` must be FALSE with the
   axis idle and disabled.
4. **Not starting on a switch:** if the axis is parked on a limit,
   HomeStatus will fault with code 4 (WRONG_LIMIT_AT_START) by design —
   jog off the switch manually first (the 750-673 gives no limit
   *direction* status, so the PLC cannot prove which switch it is on).
5. Params written: diHomeValue, diParkPosition (= diHomeValue for the
   first runs → skips MOVE_OFFSET), speeds, direction bit2.

## 4. What to observe per state (HomeStatus low byte)

| State | Watch | Pass criteria |
|-------|-------|---------------|
| 1 SEEK | BasicSpeed sign, BasicActualSpeed, axis creeping toward the **chosen** limit | direction matches bit2; speed ≈ seek setting |
| 1→2 bump | `I_*Limit` drops to 0, axis decelerates and stops (100 ms stop gate + BasicDone, then 250 ms disable dwell) | no grinding, no repeated re-strike |
| 2 BACKOFF | axis reverses at backoff speed; `I_*Limit` returns to 1, then ~200 ms more travel | switch releases within a few mm |
| 3 SETTLE | axis stationary, BasicEnable FALSE, `BasicMailboxActive` FALSE | ≥ 200 ms pause (module needs 100 ms to exit its limit special mode — constraint d) |
| 4 PRESET | `BasicActualPosition` snaps to diHomeValue; **Z only:** `diVerticalWrapR` → 0, `diVerticalOffsetR` → 0 in the same moment | no motion whatsoever in this state |
| 5 MOVE_OFFSET | (only if park ≠ home value) smooth positioning move to diParkPosition | ends stopped + disabled |
| 6 DONE | bit8 (homed) set, bit9 (busy) clear | axis disabled, quiet |
| 7 FAULT | wFaultCode | code matches the provoked condition |

Provoke-and-verify (do these on Y): (a) START with a jog bit held →
request stays pending until jog released ≥ 1 s; (b) ABORT mid-SEEK →
clean stop, FAULT 7; (c) START while sitting on a limit → FAULT 4, no
motion; (d) unplug nothing / force nothing mid-mailbox — never interrupt
PRESET.

## 5. Datum verification against the scribe mark

1. Home the axis (park = home value, so it stays at the datum-adjacent
   backoff point).
2. Command a MoveAbsolute (existing host path) to the position that should
   place the carriage at the **physical scribe mark** on the rail.
3. Measure carriage edge vs. scribe line (calipers / steel rule).
   Record the offset. Repeat at a second known mark if available.
4. If offset is consistent, fold it into diHomeValue (X/Y linear:
   counts; Z: counts within the 8,000,000 period) and re-home to confirm.

## 6. Re-home repeatability

1. Home the axis, record `BasicActualPosition` (should equal
   diHomeValue) — then move ~1 inch away, and home again.
2. Compare the *physical* position after the two homes: command the same
   MoveAbsolute target and check the carriage returns to the same scribe
   reading. Also compare the module count at the instant of the second
   bump (watch BasicActualPosition just before PRESET) — the spread
   between runs is the homing repeatability in counts.
3. Acceptance: spread ≪ your smallest meaningful traverse step. If it is
   large, halve the seek speed and repeat. On the big stage,
   986,938 counts/inch means 1000 counts ≈ 0.001 in.
4. Do at least 5 cycles on Y before trusting the pattern on X and Z.

## 7. Z (rotary) extras — LAST

1. Confirm the wrap-seed: watch `diVerticalWrapR`, `diVerticalOffsetR`
   and the reconstructed position in the same watch list through a full
   home. Both retains must change to 0 exactly when PRESET lands, and the
   reconstructed position must equal diHomeValue in that scan — no
   transient wrap increment (the xDatumPulse hook re-seeds the wrap
   detector's last-modulo memory; if you see a spurious ±8,000,000 jump,
   that hook is not wired — see CallSites.st.txt Block B3).
2. After homing, jog Z through at least one full period (8,000,000 counts
   ≈ 8.1 in) and back; the reconstructed position must be continuous
   (one clean wrap up, one clean wrap down).
3. Power-cycle test: home Z, power-cycle the coupler, confirm bit8
   (homed) is still set AND the reconstructed position still reads
   correctly (retain wrap counter survived). If not — retain memory issue,
   do not trust any retained datum until fixed.
4. Native MC3_HOME note: do **not** enable the Motor.Homing /
   Reference_Mode path on Z — module-internal homing bypasses the
   PLC-side wrap counter and silently invalidates the reconstruction.
   (It remains the recommended future path for X and Y only.)

## 8. Sign-off

- [ ] Y homes, faults 4/7 provoke correctly, repeatability recorded
- [ ] X homes, repeatability recorded
- [ ] Z homes, wrap-seed verified, full-period continuity verified
- [ ] Power-cycle retain test passed on all three
- [ ] Boot project created; power-up state confirmed IDLE, no motion
- [ ] Operators briefed: E-stop / ABORT / ControlWord differences
