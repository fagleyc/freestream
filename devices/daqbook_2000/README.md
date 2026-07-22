# daqbook_2000 — DaqBook/2000-series Tunnel-Conditions DAQ

A standalone Python driver + dark-mode PyQt6 GUI for the **IOtech
DaqBook/2005** at the USAFA subsonic tunnel, which digitizes the tunnel's
**dynamic pressure (Pdiff)**, **total pressure (Ptot)** and **temperature
(Temp)** transducer voltages. Fully self-contained; the future AeroVIS suite
composes it with the other instruments (e.g. serving tunnel q via
`DaqbookAuxSource`), but no device package imports another.

Talks to the hardware through the vendor **DaqX API** (`DaqX64.dll`, ctypes)
over Ethernet. Device identity (from the rig's LabVIEW setup):

| | |
|---|---|
| Device alias | `DaqBook2005` |
| IP | `192.168.1.125` (serial 808713) |
| CH0 `Pdiff` | differential, 0..+3 V requested → native 0–5 V (×2) |
| CH2 `Ptot` | differential, ±10 V (×1) |
| CH4 `Temp` | single-ended, 0–10 V (×1) |

Default engineering slopes come from Streamlined's `PRESSLOPvxi18.PCF`:
transducer **[220]** Pdiff 0.386949 psi/V, **[690]** Ptot 1.92604 psi/V.
Temp transmitter is **1 V = 10 °C** (scale 10, unit degC). Default scan
rate is 200 Hz; plot redraws are envelope-decimated so long windows and
high rates stay smooth.

## Quick start

```bash
# Pure simulation — no hardware, no DLL:
python run_daqbook_app.py --sim

# Real device (after one-time DaqX setup below):
python run_daqbook_app.py

# Safe read-only probe (DLL load → daqOpen → 1 s of data):
python probe_daqbook.py
```

## One-time DaqX setup (per PC)

`daqOpen` addresses the device by an alias, not by IP directly. Two pieces
must exist on the PC:

**1. The device alias** — create/list/delete it with the bundled tool (no
vendor applet needed; uses the DLL's own `daqCreateDevice`):

```bash
python daqbook_setup.py list                       # created + detected devices
python daqbook_setup.py create DaqBook2005 192.168.1.125
```

**2. The DaqX Ethernet kernel driver** (`DaqX600e.sys`) must be installed
*and loaded*. Status: `sc query DaqX600e`.

### ✅ Live-verified on the real DaqBook/2005 (2026-07-06)

`verify_daqbook_live.py`: 19,917 scans / 20 s at the 1000 Hz ADC clock,
monotonic 1 ms timebase, zero overruns. Tunnel off: Pdiff +0.0002 psid,
**Ptot 11.38 psia (correct ambient for USAFA's elevation — validates the
[690] slope end-to-end)**, Temp ≈2.4 V raw (±0.19 V noisy; set the
transmitter scale + consider filtering when known).

**Hardware constraint discovered (DaqX error 134):** single-ended channels
accept only **bipolar** ranges; unipolar is differential-only. The driver's
range picker enforces this (SE 0–10 V request → ±10 V native).

### Getting a fresh Windows 11 PC working (everything below was required once)

1. **Windows Driver Policy** blocks the 2006 IOtech kernel drivers
   (CodeIntegrity Event 3077). Remove it with
   `devices\remove_driver_policy.ps1` (Secure Boot round-trip; see script
   header) — Microsoft documents no per-driver exemption. Re-check after
   major Windows updates with `devices\check_daqx_driver.ps1`.
2. **Kernel driver for this device family is `daqbk2k.sys`** (not just
   DaqX600e): copy from `Drivers\Ethernet_x64` to `System32\drivers`,
   `sc config daqbk2k start= auto`, `sc start daqbk2k`.
3. **Device alias**: `python daqbook_setup.py create DaqBook2005 192.168.1.125`
   (or the vendor applet). Config lives at
   `HKLM\SYSTEM\CurrentControlSet\Services\Daqx\DaqBk2k0`.
4. **Registry ACL**: `daqOpen` demands Read/**Write** on that key, so
   non-elevated apps get "alias not found" (error 113/-1). One-time fix
   (elevated): grant `BUILTIN\Users` FullControl on
   `HKLM\SYSTEM\CurrentControlSet\Services\Daqx` (inheritable).
5. **Single-session device**: if `daqOpen` fails with DerrNotOnLine (2) and
   the device RSTs TCP port 50001, another client (or a stale session)
   holds it — the embedded status page at `http://192.168.1.125/` shows
   `Opened`; use its *Reset Device* link (or power cycle). Never kill a
   Python process mid-acquisition without `disconnect()`; always the GUI
   Disconnect or Ctrl-C so the session closes.

The DLL itself is found automatically in the rig's LabVIEW folder; override
with `--dll` or File → Settings.

## Architecture

```
daqbook_2000/
├── daqx.py         ctypes DaqX binding + daqx.h constants, counts↔volts
├── config.py       DaqbookConfig / ChannelConfig (JSON, PCF-seeded defaults)
├── datamodel.py    ScanRingBuffer — block-oriented, dynamic channel fields
├── device.py       Daqbook2000 — continuous scan, poll thread, sim fallback
├── emulator.py     SimCore synthetic tunnel signals
├── aux_source.py   DaqbookAuxSource → ate_balance panel.aux (q in Pa)
├── theme.py        shared dark stylesheet + validated series palette
└── app/            PyQt6 GUI: stat tiles, full-rate histories, channel table
run_daqbook_app.py  launcher        probe_daqbook.py  read-only rig probe
```

The driver owns its ring buffer (`device.ring`), so the GUI, the balance
app's aux source, and the future AeroVIS sync layer all read one stream.
Acquisition is continuous (`DaamInfinitePost`, immediate trigger) into a
circular driver buffer; a poll thread lifts out new scans, converts
counts → volts → engineering units and timestamps them against the actual
ADC clock.

## Serving tunnel q to other consumers (AeroVIS)

`DaqbookAuxSource` exposes the live stream through a tiny duck-typed
interface (`dynamic_pressure() -> Pa`, `temperature_k() -> K`), so the
future AeroVIS suite can hand tunnel conditions to any panel that wants
them without the packages importing each other:

```python
from daqbook_2000 import Daqbook2000, DaqbookConfig, DaqbookAuxSource
dev = Daqbook2000(DaqbookConfig())
dev.connect(); dev.start()
aux = DaqbookAuxSource(dev, own_device=True)   # q in Pa from Pdiff psid
```

## Tests

```bash
python tests/test_daqbook.py                            # driver, no DLL
QT_QPA_PLATFORM=offscreen python tests/smoke_daqbook_gui.py
```
