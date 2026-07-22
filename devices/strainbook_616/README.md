# strainbook_616 — StrainBook/616 Balance-Bridge DAQ

Standalone Python driver + dark-mode PyQt6 GUI for the **IOtech
StrainBook/616** at the USAFA subsonic tunnel — the internal balance
bridge readout (**N1, N2, Y1, Y2, Axial, Roll** + **Excitation**, the
Streamlined channel names). Part of the AeroVIS suite; fully standalone.

| | |
|---|---|
| Device alias | `StrainBook_0` (applet) |
| IP | `192.168.1.123` (serial 807042; reports as WaveBook/516E) |
| Kernel driver | `wavebk.sys` (installed, auto-start) |
| CH1–4 (N1,N2,Y1,Y2) | Full bridge, 10 V exc, 1 kHz filter, ±11 mV (×447) |
| CH5–6 (Axial,Roll) | Full bridge, 10 V exc, 1 kHz filter, ±32 mV (×155.8) |
| CH8 (Excitation) | excitation readback |

## Quick start

```bash
python run_strainbook_app.py --sim    # no hardware
python run_strainbook_app.py          # real device
python probe_strainbook.py            # 3-step live probe
```

Tests: `python tests/test_strainbook.py`,
`QT_QPA_PLATFORM=offscreen python tests/smoke_strainbook_gui.py`.

## Architecture facts (verified live 2026-07-06 / 2026-07-16 — hard-won, do not re-learn)

1. **Channel map**: the built-in strain conditioning is an *internal WBK16
   bank* on DaqX channels **9–16**; front-panel "CH n" = DaqX channel
   n + 8 (`STRAIN_CHANNEL_OFFSET`). Channels 0–8 are the raw WaveBook core
   (module type Wbk516A) and reject all Wbk16 options with error 67.
   LabVIEW's channel table hides this offset.
2. **Data is SIGNED int16** regardless of the `DafUnsigned` scan flag —
   0 counts = 0 V, `0x7FFF` = +FS, `0x8000` = −FS. (Verified twice: an
   offset-binary decode maps real ~100 µV bridges onto the ±range rail —
   the 2026-07-16 "all channels railed" bug.)
3. **Scan polarity is PER CHANNEL and it matters** (2026-07-16, cabled
   balance): bridge channels need `DafBipolar` — scanned unipolar they
   peg at `0x8000` ("saturated values"). The 0–10 V excitation channel
   scans **unipolar**, which shifts the data one half-span down (0 V input
   = −FS): true volts = decoded + 5. `counts_to_volts` + the driver's
   `read_excitation` branch implement exactly this.
4. **Options download at arm time, only for channels in the scan list** —
   configure options on 9–16 *and scan 9–16*.
5. **`OutSource=ReadExcVolts` monitors only the INTERNAL excitation
   banks.** With the rig's EXTERNAL supply it reads 0 V no matter what
   the supply does (2026-07-16). The external excitation is instead wired
   into CH8's differential input and measured as a plain voltage:
   `OutSource=ReadSignal`, full bridge (4-wire, no completion), ×1,
   unipolar → reads the true supply (9.86 V measured). The internal
   banks are NEVER commanded (`DcotWbk16ExcDac`/`DmotWbk16Immediate`
   stay unused; two sources on the excitation bus would fight). For the
   record if internal excitation is ever wanted: bank DACs at ch 9/13,
   apply needs **channel scope** (`DcofChannel`), and readback then
   reports (excitation − 5 V) — verified 0→10→0 V on 2026-07-06.
6. **Open channels rail at ±range** (e.g. ±11.19 mV at ×447) — the
   expected wind-off-unplugged signature, matching LabVIEW.
7. **SSH** default OFF pending verification with a connected balance
   (`DafSSHHold = 0x10` exists as a per-channel scan flag — the likely
   missing piece if SSH is wanted later).
8. **Single-session device**: like the DaqBook, a dead client wedges the
   session. Status page `http://192.168.1.123/` shows `Opened`; its
   *Reset Device* link clears it.
9. Same Windows-11 driver-policy caveat as the DaqBook — see
   `daqbook_2000/README.md`; re-run `check_daqx_driver.ps1` after major
   Windows updates (`wavebk` service must be RUNNING).

Live-verified end to end 2026-07-16 with the balance cabled and a 10 V
external supply: 200 Hz × 7 channels, bridges at −250/−200/−695/+764/+250 µV
(Axial +16.5 mV genuine offset), excitation 9.864 V — matching LabVIEW.
`diagnose_raw_counts.py` reproduces that table (raw hex + decoded volts)
for future rig debugging. Remaining rig validation: bridge polarity
(Inv sense) and SSH, against the LabVIEW Force Balance tab.

## GUI

Live tab: stat tile per channel, all bridges overlaid on one mV plot with
a slim excitation strip below (envelope-decimated redraws), software
**Tare / Clear tare**. Channels tab mirrors the LabVIEW parameter table
(bridge, range→native gain, filter, coupling, invert, SSH, per-channel
offset/unit; "0 to 10 V" range = external-excitation readback). File →
Settings for
device/acquisition/display options; configs save/load as JSON.
