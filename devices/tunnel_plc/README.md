# tunnel_plc — SSWT tunnel (Red Lion G315 gateway)

Freestream device driver + PyQt6 dark-mode GUI for the sub-sonic wind
tunnel itself: fan speed and start/stop, via the **Red Lion G315 HMI at
192.168.1.50** acting as a Modbus TCP slave (**port 502, unit 1**).

```
python run_tunnel_app.py --sim     # plant sim, no hardware
python run_tunnel_app.py          # real gateway — read-only until ARMED
python probe_tunnel.py            # READ-ONLY probe / word-order check
python -m pytest tests\test_tunnel.py
```

## Topology (from the Crimson database, SSWT_Logger_G315_v2)

```
Freestream ── Modbus TCP :502 ──> Red Lion G315 (.50)
                                    ├─ GE SRTP :18245 ──> VersaMax PLC (.31)
                                    │    fans, heater, status lights
                                    └─ RS-485 SNP ──> GE FanDrive
                                         RPM_Set (R00102), Actual_RPM (R00200)
```

We talk **only** to the G315. The FanDrive has no network path (SNP is
single-master — never attach a second serial master), and going SRTP-
direct to the VersaMax would bypass the HMI's existing logic.

## Register map (authoritative: read_mappings.txt / write_mappings.txt)

Crimson L4 gateway blocks: each element = two 16-bit holding registers,
element N at protocol address 2·(N−1).

* **Block1 — READ, elements 1–16 @ address 0 (32 registers, one atomic
  FC3 read):** RPM_Set, Actual_RPM, then the button/light booleans in
  export order (see `registers.BLOCK1_TAGS`). **No analog channels are
  in the gateway** — pressures/temps stay with the DAQ devices.
* **Block2 — WRITE, elements 101–105 @ 200:** Tunnel_Fan_Start (200),
  Tunnel_Fan_Stop (202), Cooling_Fan_Start (204), Cooling_Fan_Stop
  (206), RPM_Set (208).

### Bearing temperatures (opt-in Block1 extension — NOT yet in Crimson)

The analog bearing sensors are VersaMax **AI0007–AI0009**
(`Analog_Feedback.B1/B2/B3` in the Crimson tag database); today only the
`Bearing_Temp_Low_Light` boolean reaches the gateway. The driver already
supports the analogs behind `TunnelConfig.bearing_temps` (default
`False`). To light them up:

1. **Crimson side** (open SSWT_Logger_G315_v2 in Crimson 3):
   Communications → the Modbus TCP slave's **read gateway block (Block1,
   elements 1–16 @ address 0)** → extend it with **elements 17, 18, 19
   mapped to `Analog_Feedback.B1`, `Analog_Feedback.B2`,
   `Analog_Feedback.B3`** (in that order, direction 'Tag to Block' like
   the rest of Block1), then **re-download to the G315**. The new
   elements land at protocol addresses 32/34/36.
2. **Driver side:** enable *Bearing temperatures* in File → Settings (or
   set `"bearing_temps": true` in the config JSON). The Block1 poll then
   becomes ONE contiguous 38-register FC3 read and the snapshot carries
   `bearing_b1/b2/b3` as scaled floats (`None` while disabled).

Scaling is linear per channel from `tunnel_tags.csv`: B1 raw 955–5035 →
0–150, B2 969–4979 → 0–150, B3 930–4994 → 0–150. The unit label defaults
to **°C but the cal vintage/unit has not been confirmed on the rig** —
verify against the bearing RTD spec (0–150 could equally be °F) before
trusting absolute values; the label is editable in Settings.

**Verified live 2026-07-07** (`probe_tunnel.py`, read-only): word order
is **low_first** (RPM_Set=600 and three lit booleans all put the value
in the low word), Actual_RPM read 0 at idle, all booleans clean 0/1.
**RPM registers are Crimson fixed-point ×10** (1 display decimal):
register 600 = HMI display 60.0 RPM → `rpm_scale = 0.1`. `rpm_max` and
all driver APIs are in true engineering RPM.

## Architecture — two strictly separated classes

* **`TunnelMonitor`** (`monitor.py`) — read-only. One contiguous Block1
  read per poll (atomic snapshot), 1–2 Hz, reconnect with exponential
  backoff, `stale` flag once data is older than `stale_after_s`. Has
  **no write methods** (a test enforces this).
* **`TunnelControl`** (`control.py`) — the only write path. Refuses to
  instantiate without keyword-only `enable_writes=True`; in the GUI the
  object doesn't even exist until the operator ARMS writes. Guards:
  * RPM clamped to `rpm_max`; **all RPM writes refused while
    `rpm_max` is 0 (the shipped default)** — configure the real limit
    first.
  * Commands refused while the snapshot is stale or
    `Inverter_Fault_Light` is set. Deliberate exception: the two
    **stop** buttons bypass those interlocks (stopping is the safe
    direction) and only require a live gateway.
  * Fan buttons are momentary: write 1, hold 250 ms, write 0.
  * Every write logged (timestamp, old, new) to `write_log` and the
    GUI table.

`gateway.py` is the shared thread-safe transport (one socket for both
classes); `emulator.py` is a plant sim (fan spool-up, fault injection,
comm-failure injection, write history) used by the GUI's Simulate mode
and the tests.

## Troubleshooting: writes rejected (seen live 2026-07-07)

Pressing any control button raised
``ExceptionResponse(function_code=144, exception_code=2)`` =
**FC16 write rejected, ILLEGAL DATA ADDRESS** — the slave serves the
registers for reading but refuses writes there. Reads at the same
addresses work, so the map is right; the block just isn't writable.
Fix is on the Crimson side (open the SSWT_Logger database in Crimson
3):

1. Communications → the Modbus TCP slave driver: **'Read Only' must be
   NO** (it defaults to read-only protection in some setups);
2. the Block2 gateway block (elements 101–105) must have direction
   **'Block to Tag'** (incoming writes update the tags) — Block1 stays
   'Tag to Block';
3. re-download to the G315.

Then run ``python tunnel_write_check.py --arm`` — it writes the SAME
retained RPM_Set value back to Block2 (a plant no-op), reports whether
the slave accepts FC16 and/or FC6, and confirms the readback. The
driver itself now tries FC16 and falls back to FC6 automatically, and
write rejections surface in the status bar with this diagnosis instead
of a traceback.

## Manual write-validation procedure (NOT yet performed)

The write path is sim-tested only. Before first operational use, with a
person at the console and the test section clear:

1. **RPM setpoint, fan OFF:** set `rpm_max` (Settings), arm, command a
   small RPM (e.g. 100). Confirm the HMI's setpoint display follows and
   `RPM_Set` in Block1 reads back the same value. This validates Block2
   element 105 end-to-end without motion.
2. **Momentary pulse (TODO from code):** command *Cooling fan START*
   (the benign load) and watch the VersaMax/HMI: the button must latch
   the fan on from a single 250 ms pulse, exactly like a touchscreen
   press. If it needs a longer hold, raise `button_hold_ms`; when
   satisfied, set `momentary_verified` in Settings to silence the
   per-pulse warnings.
3. **Tunnel fan start/stop:** only after 1–2 pass, low RPM first, and
   verify *stop* works before anything else matters.
4. Record the verified `rpm_max` in a saved config JSON.

## Files

| file | role |
|---|---|
| `registers.py` | Block layouts, L4 encode/decode, `TunnelSnapshot` |
| `gateway.py` | Modbus transport (+ `FakeClient` for tests) |
| `monitor.py` | `TunnelMonitor` — read-only poller |
| `control.py` | `TunnelControl` — guarded writes |
| `emulator.py` | `SimGateway` plant sim |
| `config.py` | `TunnelConfig` (JSON, safety knobs) |
| `app/` | PyQt6 GUI (Monitor view + armed Control section) |
| `SSWT_tunnel_architecture.md` | extracted topology/safety notes |
| `read/write_mappings.txt` | authoritative Crimson gateway exports |
