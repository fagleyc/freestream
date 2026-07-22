# ate_balance — ATE External Balance TMS Client

A standalone Python client for the **ATE Aerodynamic Test Equipment 6-component
underfloor (external) balance** at the USAFA wind tunnel. It speaks the TCP/UDP
protocol of the balance's **OGI** control PC and provides a PyQt6 dark-mode GUI
that matches the **Streamlined** project, with live loads, motion control, and
dwell-averaged points. The app shows **raw wind-axis loads only** — coefficient
reduction (reference geometry, air density) is owned by the Freestream suite.

Protocol source of truth: `AID-010-10015-1.pdf` §6 "Client Communication
Facilities", cross-checked against `OGI/Source/USAFA_Code.pas`.

---

## Quick start

```bash
pip install -r requirements.txt          # numpy, PyQt6

# 1) Pure simulation — no hardware, no network:
python run_ate_app.py --sim

# 2) Against the bundled Python emulator (two terminals):
python -m ate_balance.emulator --tms-ip 127.0.0.1
python run_ate_app.py --ip 127.0.0.1

# 3) Against the real OGI on the rig network (static IP 192.168.1.60, the default):
python run_ate_app.py --ip 192.168.1.60
```

By default the client **listens** on TCP 3040 (the OGI dials in, per the
manual). Click **Connect**; the lamp shows `WAITING FOR OGI` → `LINKED` once the
OGI connects and the LOADS stream arrives.

### Testing with `OGI_Sim.exe`
`OGI_Sim.exe` is the rig's own simulator. Point its `OGI.ini` `[TMSC] IP` at the
machine running this client, start it, then run option 3 above with `--ip` set
to the `OGI_Sim` host. The client's `Trigger` button sends `TMS_CONNECT` to
prompt the OGI to (re)dial. If `OGI_Sim` is on the same PC, use `--ip 127.0.0.1`.

---

## Architecture

```
ate_balance/
├── protocol.py     pure wire protocol: brace framing, M/R messages, LOADS codec
├── datamodel.py    BalanceFrame, MasterFrame+RingBuffer (≈wtdaq),
│                   TunnelConditions/TestCase/ReducedPoint (≈Streamlined)
├── reduction.py    frame merging + dwell averaging (raw wind-axis loads)
├── config.py       AteConfig — endpoints, rated load maxima, JSON, OGI.ini seeding
├── device.py       AteBalanceDevice — sockets + threads + simulation fallback
├── emulator.py     OgiSimCore (logic) + FakeOGI (real-socket OGI stand-in)
├── aux.py          AuxSource ABC + SimAuxSource  ← DAQbook integration point
├── theme.py        Streamlined dark stylesheet + validated 6-channel series palette
└── app/            PyQt6 GUI
    ├── plots.py           pyqtgraph LoadBars + full-rate TimeHistory widgets
    ├── settings_dialog.py File → Settings… (network/sampling/display/rated maxima)
    └── panels/            connect / live (bar graph) / motion / run (time history)
run_ate_app.py      launcher
tests/              protocol, integration, and headless-GUI tests
```

The driver mirrors the `wtdaq` `DAQbook2000` driver shape (`on_frame`/`on_status`
callbacks, `connect`/`start`/`stop`, `frame_count()`) so it can later drop into
the AeroSENSE/`wtdaq` framework unchanged.

## Protocol summary

| Channel | Transport | Default | Direction | Payload |
|---|---|---|---|---|
| TMSC control | TCP | 3040 | OGI dials client | `{M<serial>:CMD args}` / `{R<serial>:reply}` |
| TMSD data | UDP | 3041 | OGI → client | `b"LOADS"` + 6×float32 (+int32 sync), big-endian |
| OGIT trigger | UDP | 3042 | client → OGI | `TMS_CONNECT` |

Loads are wind-axis, order **Lift, Pitch, Drag, Side, Yaw, Roll** (N, N·m).
Commands: `ZERO`, `TAKE_SAMPLE`, `LOCK_BAL`, `UNLOCK_BAL`, `GET_LOCK_STATUS`,
`GET_POSITIONS`, `GOTO_YAW_POS`, `GOTO_INC_POS`, `GET_FILTERS`, `STOP_ALL_MOTION`.

## Data structure mapping (→ Streamlined)

The reduced layer uses Streamlined's exact field names so a run drops straight
into the Streamlined tooling: WRF loads
`lift_forces/drag_forces/side_forces/roll_moments/pitch_moments/yaw_moments`
and `TunnelConditions(Q, ...)`. Coefficients are **not** formed here — the
Freestream suite owns the reference geometry and does that reduction. Dwell
points export to CSV or to a Streamlined-shaped `TestCase` `.npz` (Run panel →
Export).

## Rated load maxima

`AteConfig.max_loads` holds a per-channel rated maximum keyed by the six wire
axis names `Lift, Pitch, Drag, Side, Yaw, Roll` (N for forces, N·m for
moments; `0.0` = no limit configured). Edit under File → Settings… → "Rated
load maxima". The suite streams utilization bars against these; the standalone
Live panel shows an overstress hint when a smoothed load exceeds a nonzero
maximum.

## DAQbook integration (stub this pass)

Per the agreed scope, the DAQbook is wired only as *structure*: implement
`aux.AuxSource` around the existing `wtdaq.devices.daqbook2000.DAQbook2000`
driver and pass it to the panel. The app already calls
`AuxSource.dynamic_pressure()` when building each frame (currently served by
`SimAuxSource`), so swapping in a real `DaqbookAuxSource` is the only change —
see the worked stub at the bottom of `aux.py`.

## ✅ Verified on the real rig (2026-07-06, `probe_ate_rig.py`)

Probed the live OGI at **192.168.1.60** (read-only queries only):

* TMS_CONNECT trigger → OGI dialled back in <10 ms; control link stable.
* **LOADS stream: 300 Hz, 29-byte packets (no sync word)** — the USAFA-build
  variant, as suspected. `device.last_had_sync == False`.
* `GET_FILTERS` → `300Hz 9` ×6 channels (matches the 300 Hz stream rate).
* `GET_POSITIONS` / `GET_LOCK_STATUS` answered correctly.
* Unloaded loads read ≈0 (|mean| < 0.1, sd ≈ 0.02–0.17) — consistent with N/N·m.

**Still to confirm once physically at the rig:** apply a known weight and check
the `Lift` readout magnitude to lock in the N / N·m units assumption.
`python probe_ate_rig.py` re-runs the whole check safely at any time.

## Tests

```bash
python tests/test_protocol.py        # pure protocol (no hardware)
python tests/test_integration.py     # device ↔ FakeOGI over real sockets + sim
QT_QPA_PLATFORM=offscreen python tests/smoke_gui.py            # GUI in sim mode
QT_QPA_PLATFORM=offscreen python tests/smoke_gui_emulator.py   # GUI ↔ emulator
```
(or `pytest tests/` — the `test_*.py` files are pytest-discoverable.)
