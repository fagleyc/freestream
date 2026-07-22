# balance_cal — Force/Moment Balance Calibration GUI

Python port of the MATLAB `FB_Cal_GUI/ForceCal.mlapp` calibration app.
Acquires bridge voltages from an analog-input DAQ while dead-weight
loads are applied to a balance, and writes a **Voltage Calibration File
3.1** (`.vol`) — the same format as the example
`2025_06_06_2 100 lb.vol` — consumable by the device drivers'
`balcal.read_vol_file`, freestream's Forces monitor, and Streamlined.

## Run

```
python run_balcal_gui.py             # NI USB-6351 (default)
python run_balcal_gui.py --sim       # no hardware, simulated stream
python run_balcal_gui.py --backend strainbook
```

Or from **freestream**: `Advanced ▸ Balance Calibration…` — when
freestream's balance DAQ is connected the window shares that live
stream; otherwise it opens its own connection.

## Devices

Drivers are loaded from `../devices` (nothing is duplicated here):

| Backend | Driver | Notes |
|---|---|---|
| `ni6351` (primary) | `ni_usb_6351.NiUsb6351` | NI-DAQmx; device alias from NI-MAX (e.g. `Dev2`) |
| `strainbook` | `strainbook_616.Strainbook616` | daqx DLL; external excitation read back on CH8 |

Both expose the same lifecycle (`connect/start/stop/disconnect`), a
continuously-filling ring buffer, and per-channel raw-volt fields — the
GUI is agnostic via `balcal_gui/daq.py`.

## Calibration procedure (mirrors the MATLAB app)

1. **Measurement Setup** — pick driver/device (or Simulate), balance
   type (Force = 5F/1M, Moment = 1F/5M), edit channel physical/range
   assignments, enter operator, serial number, outer diameter, max
   loads and element distances.
2. **Calibration Procedure** — for each load orientation
   (`N1_pos` … `Mx_neg`; guide images from `FB_Cal_GUI/` show the
   rigging): hang the dead weight, press **Acquire Test Point**, enter
   the applied weight. The entered weight is multiplied by the moment
   arm (1.0 for direct forces; for moment-balance pitch/yaw the weight
   hangs at the opposite station so the arm is the station separation
   dx1 + dx2, i.e. both distances to the balance center summed; roll
   uses the row-6 distance, default 2 in) and
   the six bridges + excitation are averaged over the selected window
   (fresh samples only). Bracket each sweep with 0-load points — the
   reduction uses them for the zero offset. Loads exceeding the entered
   max produce a warning.
3. **Live cal plot** — beside the measurement table: applied load vs
   the primary bridge voltage for the current orientation, ±1σ error
   bars from each dwell. Points whose std exceeds 3× the orientation's
   median are drawn in warning orange (weight still swinging — delete
   and re-acquire); a dashed linear trend gives a quick sanity check.
4. **Write to File…** — saves the `.vol` (format 3.1).
   **File ▸ Load .vol for editing…** reloads a saved calibration —
   metadata, max loads, distances, and every point — so you can delete
   bad points, append new ones, and rewrite (per-point stds are not
   stored in the format, so reloaded points show no error bars).
   **File ▸ Open device panel…** opens the driver's native app (live
   tiles, channel config, output/trigger) sharing the calibration
   window's connection — works for both the NI 6351 and StrainBook.
5. **Cal Summary** — runs the same least-squares reduction the
   consumers use (`balcal.calc_coeffs`, Linear/Quadratic/Cubic) and
   reports per-element R², RMS bias and the calibration matrix; the
   report can be saved as text. Volts are stored raw (never tared);
   normalization by excitation and zero-offset removal happen in the
   reduction, exactly as `read_vol_file` implements it.

## Package layout

```
balcal_gui/
  session.py    calibration session model, orientations, moment-arm logic
  volfile.py    .vol 3.1 writer (round-trip vs devices/*/balcal.py)
  daq.py        device-agnostic acquisition shim over ../devices drivers
  report.py     least-squares summary / text report
  app/          PyQt6 GUI (house dark theme, embeddable in freestream)
tests/          pytest: format round-trip, session logic, sim DAQ, GUI smoke
```

## Tests

```
python -m pytest tests -q
```

All tests run without hardware (NI sim mode + offscreen Qt). A live
smoke test against a connected NI device:

```
python -c "import sys; sys.path[:0]=['.','../devices']; \
from balcal_gui.daq import BalanceDaq; from balcal_gui.session import BalanceKind; \
d=BalanceDaq('ni6351', device_name='Dev2'); d.connect(BalanceKind.FORCE); \
print(d.acquire(1.0, BalanceKind.FORCE).means); d.disconnect()"
```
