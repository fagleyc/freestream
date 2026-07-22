# SSWT Tunnel Control Architecture (from SSWT_Logger_G315_v2.cd3)

Extracted 2026-07-07 from the Red Lion G315 database (download IP 192.168.1.50).

## Communications topology

```
                        Ethernet (192.168.1.x)
  ┌────────────────────────┬──────────────────────────┐
  │                        │                          │
Red Lion G315 (.50) ── GE SRTP master, TCP 18245 ──> VersaMax PLC (.31)
  │                                                   - tunnel fan start/stop
  │ RS-485 Comms Port A (SNP protocol, slot 1)        - cooling fan, heater
  └──> "FanDrive" (GE SNP device)                     - analog feedback AI0001-15
       - RPM_Set       = R00102  (WRITE - speed command)
       - Actual_RPM    = R00200  (read)
```

- **VersaMax PLC**: GE SRTP over Ethernet, IP 192.168.1.31, port 18245.
  Timeouts: 5000/2500/200 ms. (Not the 90-30 — the tunnel I/O PLC is a VersaMax.)
- **FanDrive**: GE SNP over RS-485 serial from the HMI. Drive speed command and
  RPM feedback. NOT directly reachable over the network — only via the HMI.
- The G315 also computes derived values internally (Mach steps, Reynolds number,
  pressure conversions x133.322368 mmHg->Pa) — reproduce these in Freestream
  rather than reading them.

## Key control tags (WRITE - treat with care)

| Tag | Address | Function |
|---|---|---|
| RPM_Set | FanDrive.R00102 | Fan speed command |
| Tunnel_Fan_Start_Button | VersaMax.M00001 | Start tunnel fan |
| Tunnel_Fan_Stop_Button | VersaMax.M00002 | Stop tunnel fan |
| Cooling_Fan_Start_Button | VersaMax.M00004 | Start cooling fan |
| Cooling_Fan_Stop_Button | VersaMax.M00005 | Stop cooling fan |
| Bearing_Heater_On_Button | VersaMax.Q00053 | Bearing heater |

## Key monitoring tags (read-only)

| Tag | Address | Notes |
|---|---|---|
| Actual_RPM | FanDrive.R00200 | Fan RPM feedback |
| Analog_Feedback.V1-V3 | VersaMax.AI0001-03 | Winding temps, raw 1019-5000 counts |
| Analog_Feedback.W1-W3 | VersaMax.AI0004-06 | Winding temps, raw 1000-5000 counts |
| Analog_Feedback.B1-B3 | VersaMax.AI0007-09 | Bearing temps (per-channel cal) |
| Mach_Speed.Diff_Pressure | VersaMax.AI0012 | Raw +/-32000 counts |
| Mach_Speed.Atmospheric_Pressure | VersaMax.AI0013 | Raw +/-32000 counts |
| Mach_Speed.Tunnel_Temperature | VersaMax.AI0015 | Raw 130-10107 counts |
| Fan_Running_Light | VersaMax.M00103 | Status |
| Inverter_Fault_Light | VersaMax.M00110 | Status |
| Oil_Level_Low_Light | VersaMax.M00109 | Status |
| Bearing_Temp_Low_Light | VersaMax.M00111 | Interlock indicator |
| Stop_Flip | VersaMax.I00019 | Physical stop input |

Full 52-tag list with scaling in tunnel_tags.csv.

## Integration options for Freestream

1. **VersaMax direct (Ethernet)**: talk GE SRTP to 192.168.1.31:18245 from
   Python. Gets all AI channels + status/command bits without touching the HMI.
   (.31 was absent from the ARP scan — verify the PLC is on and reachable.)
2. **FanDrive (RPM) has no network path** — it hangs off the HMI's RS-485 port.
   To read/write RPM over the network, add a Modbus TCP slave + gateway block
   to this .cd3 in Crimson and re-download (enable Support Upload while at it).
   The HMI then bridges Ethernet Modbus <-> serial SNP for you.
3. Recommended: do (2) for everything — one Modbus interface, one register map,
   HMI keeps enforcing existing logic. Use (1) later if polling rates demand it.

## Safety notes

- RPM_Set, fan start/stop, and heater tags command real machinery. Develop
  read-only first; gate writes behind software interlocks + operator confirm.
- SNP over RS-485 is single-master: never attach a second serial master to the
  FanDrive line while the HMI is connected.
