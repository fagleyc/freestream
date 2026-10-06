# lswt_traverse — South LSWT 3-axis traverse (IDC SmartStep23)

Standalone AeroVIS device driver + PyQt6 dark-mode GUI for the South
LSWT probe traverse: three **IDC SmartStep23** microstepping
SmartDrives on ONE RS-232C daisy chain (9600 8N1, XON/XOFF), unit
addressed on the rig as **drive 1 = Z vertical, 2 = Y lateral,
3 = X axial** (photo `LSWT-S Traverse Photos/stepper_drivers.jpg`).

```
python run_lswt_traverse_app.py --sim          # chain emulator
python run_lswt_traverse_app.py --port COM7    # real chain
```

## Why it is much simpler than the SWT traverse

The SWT WAGO traverse gives the host only direction bits, so that
driver runs a bang-bang position loop with counts calibration, wrap
unwrapping and limit choreography. The SmartSteps position
**themselves**: the host sends a buffered profile
(`AC/DE/VE/DA…GO`) and the drive runs it. The actuator gear ratios
were configured into each drive via the keypad / Application
Developer, so `PA1` already answers in **inches**. What is left for
the host is exactly what this driver does:

1. **Set current position to home** — jog to the reference spot,
   press *Set home here* (wire `SP<datum>`, normally `SP0`).
2. **Software limitations** — per-axis soft travel limits, edited on
   each axis card, enforced HOST-side on every absolute target, and a
   jog on a referenced axis is auto-stopped the moment it crosses one.

Absolute moves stay locked until an axis is referenced: a SmartStep
wakes up reading 0.000 wherever it stands, so an unreferenced "go to
+5" would be a blind lunge. The reference is per-drive-power-cycle.
Jogs are always allowed — that is how you reach the reference spot.

## Wire protocol (IDeal, manual ch. 8)

Source of truth: `SmartStep23_User's_Manual.pdf` (IDC / Danaher
Motion, P/N PCW-5008). Commands are two UPPERCASE letters with an
optional unit-address prefix (`2PA1`); an unaddressed command
broadcasts. Responses are `*`-prefixed and CR-terminated. **RS-232C
echo is ON** (mandatory for daisy chaining) — every read starts with
our own echoed bytes, which `protocol.extract_response` discards.

| used here | meaning |
|---|---|
| `<n>PA1` | position (inches) → `*+1.000` |
| `<n>SA1` / `SD1` / `SS` | axis / drive / system status (4-digit hex) |
| `<n>MN` | model probe at connect (`*SmartStep23`) |
| `<n>AC DE VE DA … GO` | buffered move profile |
| `<n>MC+ … VE±v GO` | continuous move (hold-to-jog); sign of VE = direction |
| `<n>S` / `S` | decel-stop one unit / whole chain (the E-stop) |
| `<n>K` | instant kill, NO decel ramp — panic only |
| `<n>SPr` | **set position** — the whole homing story |
| `<n>EAi` | amplifier enable/disable |

## Architecture

```
lswt_traverse/
├── protocol.py   IDeal wire protocol: framing, echo-tolerant parsing,
│                 SA/SD/SS status bits
├── config.py     TraverseConfig / AxisConfig — port, soft limits,
│                 speeds, JSON defaults (~/.lswt_traverse/defaults.json)
├── device.py     LswtTraverseDrive — serialized transactor + monitor
│                 thread (PA/SA poll, move supervision, soft-limit jog
│                 fence, drive-fault surfacing), ScanRingBuffer stream
├── emulator.py   SimChain — byte-level chain stand-in, echo included,
│                 behind a pyserial-shaped interface
├── datamodel.py  ScanRingBuffer (house pattern)
├── theme.py      Streamlined dark palette + wheel guard
└── app/          PyQt6 GUI: axis cards (readout, jog, move, Set home,
                  soft limits), E-STOP, position history, status log
run_lswt_traverse_app.py   launcher (in devices/)
tests/test_lswt_traverse.py
```

## Hardware notes

* One COM port serves the chain; set it on the connection bar and
  *Set as Defaults*. Chain wiring per manual ch. 8-20 (host TX → unit
  1 RX, unit 1 TX → unit 2 RX, …, last TX → host RX).
* Sign conventions (photo `traverse_actuators_annotated.jpg`):
  X + downstream, Y + right looking downstream, Z + up.
* The default ±12" soft limits are placeholders — measure the real
  travel on the rig, set the limits on the cards, *Set as Defaults*.
* Hardware limit switches, if wired NC into the drives, stop the
  drive itself; the monitor surfaces the latched SA bits as
  "± limit switch" on the axis card.
* `Tool_SSWT_Traverse/` in this folder is the old TunnelVision tool
  kept for reference only — it drives the **SWT WAGO** traverse over
  Modbus and does not match this hardware.

## Verify on the rig (first connect)

1. `python run_lswt_traverse_app.py --port COMx` — the connect probe
   (`MN` to units 1/2/3) must name all three drives; a silent unit is
   wiring or a drive with echo OFF.
2. Jog each axis briefly and check the direction sense against the
   annotated photo; if an axis jogs backwards, swap that actuator's
   sense at the drive (or invert its wiring) — the driver deliberately
   adds no sign-flipping layer on top of the drive's own units.
3. Jog to each reference spot, *Set home here*, then walk the soft
   limits in with real travel measurements and *Set as Defaults*.
