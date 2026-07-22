# lswt — North & South LSWT fan drives (ABB ACS530)

AeroVIS device driver + PyQt6 dark-mode GUI for the two Low-Speed Wind
Tunnel fans. Each tunnel's fan runs on an **ABB ACS530 VFD** (the
deployed C# calls it "ABB530"/"ACB530") over **Modbus TCP, unit/slave
1**. Everything protocol/calibration here was extracted from the
deployed C# source
`Tool_LSWT_Flow_Velocity\HwControllerVelocityLSWT_ACB530.cs`
(TunnelVision, on the control-room PC).

```
python run_lswt_app.py --tunnel north --sim   # fan sim, no hardware
python run_lswt_app.py --tunnel south         # real drive — ARM to act
python -m pytest tests\test_lswt.py
```

## Protocol (wire vs FieldTalk addresses)

The C# used the FieldTalk Modbus library, which counts registers from
**1** — so every C# address is +1 vs the wire (the same lesson as this
repo's other C# ports: AC Delta, traverse). Wire = FieldTalk − 1:

| wire | FieldTalk (C#) | dir   | meaning                                       |
|------|----------------|-------|-----------------------------------------------|
| 0    | 1              | write | **Control**: `1150` = STOP, `1151` = START (ABB drives-profile control words; FC6). C# lines 233, 237–238 |
| 1    | 2              | write | **Reference**: 0–20000 scales 0–full speed; full speed = **60 Hz** motor. **The C# wrote the NEGATIVE of the scaled value** (line 191: `writeSingleRegister(slave, 2, (short)-rpmScaledTo20000)`) — a direction convention on these fans, preserved via `LswtConfig.reference_sign = -1`. FC6 |
| 102  | 103            | read  | **Actual speed**: current output frequency × 10 (0–600 = 0–60.0 Hz; e.g. register 452 = 45.2 Hz). C# lines 105–111, 235 |

Example: 30 Hz → reference counts `20000 × 30/60 = 10000` → wire value
**−10000** (`0xD8F0`) with the default sign.

* **Drive IPs are NOT in the C# source** (runtime-configured XML).
  `192.168.0.1` is a **placeholder** — **TODO(Casey): set the real
  North/South drive IPs** on the connection bar, then *Set as
  Defaults* (per-tunnel `~/.lswt/defaults_north.json` /
  `defaults_south.json`; `LSWT_DEFAULTS` env var overrides the
  directory).
* The C# reconnected per transaction (open → one register → close,
  every time — slow). This driver holds **one persistent pymodbus
  client** (`drive.AbbAcs530`), thread-safe, and wraps every pymodbus
  error into the typed `LswtError` (load-bearing pattern in this repo:
  an unwrapped pymodbus timeout once killed a control thread live).

## Calibration (measured, ported verbatim)

`calibration.FPS_AT_HZ` is the C# `ftPerSecVelocityToMotorHertz` table
(HwControllerVelocityLSWT_ACB530.cs lines 59–65): a **61-point
measured curve**, index = motor Hz 0..60, value = tunnel velocity in
ft/s — "Data obtained from LSWT experimental data" (C# comment).
Endpoints: 0 Hz → 0 ft/s, 60 Hz → **105.6851 ft/s**.

`hz_to_fps()` / `fps_to_hz()` interpolate linearly (`np.interp`; the
table is strictly monotonic so the inverse is exact) and clamp at the
ends. Unit conversions: the C# `SpeedUnitsConversion` maxima are
{mps 32.21282, fps 105.6851, kph 115.9661, Mach 0.09466, mph 72.06447}
and the C# converted by ratio of maxima. Here **fps→m/s / km/h / mph
use the exact physical factors** (they match the C# maxima to <0.01% —
those maxima are just rounded conversions of 105.6851 ft/s) and
**Mach uses the C# ratio** (0.09466/105.6851 — the tool's calibrated
tunnel-conditions value).

RPM is deliberately not displayed: the motor pole count isn't in the
source, so Hz is the honest motor quantity (the C# itself conflated
"RPM" and Hz in its variable names).

## Safety model

* **ARM gating** (mirrors tunnel_plc): Start Fan / Stop Fan / Apply
  Setpoint stay disabled until the operator explicitly ARMS fan
  control (with a confirm dialog on live hardware). The **E-STOP is
  always live** — STOP word + zero reference written immediately from
  the calling thread, never queued.
* **Host-side ramp** (`ramp_hz_per_s`, default 2 Hz/s): the commanded
  reference always ramps toward the setpoint and **never step-jumps
  the fan**. This deliberately REPLACES the C#'s crude protection
  (`setMotorCntrlrVelocity` lines 158–172: any requested change >2 ft/s
  → command reference **0**, i.e. slam the fan toward zero).
  `fan_start()` anchors the ramp at the current actual speed.
* **`fan_stop()`** writes the STOP word **and zeroes the reference**.
* **Comm loss = alert, NOT auto-stop**: no successful poll within
  `stale_after_s` → status STALE (red in the GUI), but the fan is
  deliberately not stopped — the ACS530 holds its last reference
  safely on its own, and auto-stopping would turn a transient network
  blip into an aborted run / an uncommanded flow change mid-test. The
  physical console and E-STOP remain the backstop.
* `connect()` is **read-passive** (no writes), so a host reconnect
  never disturbs a fan already running from a previous session.

## Sim mode

`--sim` / the Simulate checkbox swaps in `emulator.SimAcs530`: a
first-order fan model (time constant ~3 s, `sim_tau_s`) honoring
start/stop and the reference, so the full GUI (gauge, ramp, strip
charts, velocity calibration) runs without hardware.
`python -m lswt._gui_screenshot` renders an offscreen screenshot.

## First-live-run checklist

1. Set the real drive IP (connection bar) → *Set as Defaults*.
2. Connect (read-passive) — confirm the **actual-Hz register**: the
   gauge must track the console/drive display (register = Hz × 10).
3. **Verify the reference sign at a tiny reference**: arm, setpoint
   ~1–2 Hz, Start Fan, and confirm the fan turns the NORMAL direction.
   A wrong `reference_sign` commands REVERSE — flip it in Settings →
   Advanced if so.
4. Confirm the start/stop words: Start Fan spins up, Stop Fan coasts
   down and the reference reads 0.
5. Only then run real setpoints; tune `ramp_hz_per_s` to taste.
