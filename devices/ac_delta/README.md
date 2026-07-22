# ac_delta — ARC Crescent (SSWT Sting) Drive

Standalone Python driver + dark-mode PyQt6 GUI for the crescent's dual
**Delta C2000** drives, Modbus TCP, one per axis:

| Axis | IP | Stop value | Direction |
|---|---|---|---|
| Alpha | `192.168.1.11` | 17 | normal |
| Beta | `192.168.1.12` | 33 | **inverted** (wire tables swapped, as deployed) |

## Quick start

```bash
python run_crescent_app.py --sim    # physics sim, no hardware
python run_crescent_app.py          # real drives
python probe_crescent.py            # READ-ONLY live check (no motion)
```

Tests: `python tests/test_crescent.py`,
`QT_QPA_PLATFORM=offscreen python tests/smoke_crescent_gui.py`.

## Protocol (from the deployed C# `FTool_SSWT_Sting` / `HwControllerStingSSWT`)

* Read encoder: holding register **8714** (1 reg, signed 16-bit).
* Command register **8193** (write single):
  forward steps 1–5 = `4370 4626 4882 5138 5394`,
  reverse steps 1–5 = `4386 4642 4898 5154 5410`,
  stop = `17` (Alpha) / `33` (Beta).
* Motion is **host-closed-loop**: read angle → pick speed step from
  remaining distance → write step → stop inside tolerance.
* Angle cal (two-point): `angle = angle_high − (encoder_high − encoder) /
  clicks_per_degree`. **The real constants live in the rig's XML tool
  config** — enter them in the Calibration tab (or capture two known
  angles) before trusting angles. Until then the GUI flags UNCALIBRATED
  and shows raw encoder counts.

## Operating model

* **Uncalibrated axes show raw encoder counts** (big readout + plot) and
  are **jog-only** — angle targets unlock per axis once calibrated, and
  the synchronous move once both are.
* **Jog** is hold-to-run (press = move, release = stop) with a per-axis
  speed step 1–5. While calibrated, jogs auto-stop at the soft limits;
  uncalibrated jogs have no limit protection — watch the hardware.
* **Calibration = limit-switch referencing**: enter the gearing constant
  (clicks/degree from the rig XML config), jog onto the limit switch,
  type the switch's known angle, press *Set current position*.
* **pymodbus "transaction_id" errors**: the C2000 PLC occasionally answers
  late; pymodbus skips the stale frame and recovers. The driver retries
  the read once and demotes that library log to silence — real failures
  still surface through the drive watchdog.

## Improvements over the C# implementation

* **Persistent Modbus connections** (the C# reconnected before every
  read/write) and **change-only step writes** (the C# rewrote the step
  every tick) — less bus churn, lower latency.
* **50 ms loop** (C#: 100 ms) and **configurable deceleration bands**
  (File → Settings): defaults `[0.5, 1.0, 1.75, 2.5]°` brake later than
  the C#'s `[1.0, 1.5, 2.25, 3.0]°`. Tune against the LabVIEW profile —
  the loop period, bands, and max step are all live-adjustable.
* **Synchronous dual-axis moves**: one control thread services both axes
  every tick; `move_to(alpha=…, beta=…)` starts both in the same tick and
  each brakes on its own distance.
* Safety: soft travel limits checked before motion, per-axis Stop +
  global **E-STOP** (immediate, not queued), Modbus watchdog (consecutive
  errors → stop all + drop the axis).

## Live status (2026-07-06): map verified, encoder fixed

The C# map is correct **with a one-register offset on the encoder**:
FieldTalk counts register references from 1, so C# "8714" is wire address
**8713** (pymodbus is 0-based). Verified live with single-register reads
(block reads spanning undefined C2000 registers fail whole-block with
Illegal Data Address — scan with count=1 only):

* **8713** — encoder, signed 16-bit, axis-unique ✓ (8714 is a constant 0,
  which is why the first driver build read zeros).
* **8192 (0x2000)** — the Delta C2000 **control word** (LabVIEW-confirmed;
  manual page in `info/`). Bits: 1-0 run/stop (01=stop, 10=run), 5-4
  direction (01=FWD, 10=REV), 7-6 accel/decel set, 11-8 step-speed 0-15,
  12 enable. The C# "step values" are full control words
  (0x1112 = run+FWD+step1+enable, …); the stop words 17/33 are both plain
  stops (direction bit differs, harmless).
* LabVIEW speed-band logic (screenshot in `info/`) works in **encoder
  counts**: remaining >500→step 5, >400→4, >200→3, >100→2, else 1 —
  convert with clicks/degree when tuning the degree bands in Settings.
* C2000 telemetry confirms the devices: 0x2105 DC bus 370.0 V, 0x220E IGBT
  ≈23 °C. The RedLion `.cd3` is encrypted; not needed.
