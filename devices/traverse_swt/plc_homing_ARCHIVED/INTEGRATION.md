# Integrating the homing FB into LVW_V3_2021.pro
### WAGO-I/O-PRO CAA (CoDeSys 2.3) — click-by-click, first-timer friendly

All file references below are in this folder
(`...\devices\traverse_swt\plc_homing\`).

> **Before you start:** make a copy of `LVW_V3_2021.pro` (plain file copy
> in Explorer). CoDeSys 2.3 has no undo across sessions.

---

## 1. Open the project

1. Start **WAGO-I/O-PRO CAA** (CoDeSys 2.3).
2. `File → Open…` → select `LVW_V3_2021.pro`.
3. The **Object Organizer** is the pane on the left with four tabs at the
   bottom: **POUs**, **Data types**, **Visualizations**, **Resources**.
   (If it is hidden: `Window → Organizer`.)

## 2. Add the function block POU

1. Click the **POUs** tab.
2. Right-click in the empty area of the tree → **Add Object…**
3. In the *New POU* dialog:
   - **Name of the new POU:** `FB_AxisHoming`
   - **Type of POU:** select **Function Block**
   - **Language of the POU:** select **ST**
   - OK.
4. The editor opens with two panes: the **declaration part** (top) and the
   **body** (bottom).
5. Open `FB_AxisHoming.st.txt` in a text editor.
   - Copy everything from `FUNCTION_BLOCK FB_AxisHoming` down to the last
     `END_VAR` (i.e. all VAR_INPUT / VAR_IN_OUT / VAR_OUTPUT / VAR /
     VAR CONSTANT sections) into the **top** pane, replacing what the
     wizard generated. Keep the leading comment header too — it is legal
     above `FUNCTION_BLOCK`.
   - Copy everything after the `(* BODY *)` banner (from
     `rtStart(CLK := xStart);` to the last timer call and output lines,
     but **not** the `END_FUNCTION_BLOCK` line — 2.3 adds that implicitly)
     into the **bottom** pane.
6. `File → Save`.

> **Pin-name check (do this now):** `Window → Library Manager`, select
> `Stepper_03.lib`, click `MC3_SetPosition` and confirm its formal input/
> output names. Three lines near the bottom of the FB body are marked
> `VERIFY ON RIG: pin name` — fix them here if the formals differ from
> `xStart / diPosition / Stepper`, and the Done/Busy/Error reads in the
> ST_PRESET branch (`SetPos.xDone` etc.).

## 3. Add the global variables

1. Click the **Resources** tab.
2. Expand **Global Variables**, double-click **Global_Variables** (the list
   that already contains `ControlWord AT %MW0`).
3. Open `GlobalDecls.st.txt`. Paste the contents of **SECTION 1** *inside*
   the existing `VAR_GLOBAL … END_VAR` block (i.e. just the variable lines,
   before the final `END_VAR`) — or append the whole block after it; both
   are legal, duplicates are not.
4. Right-click **Global Variables** → **Add Object…** → name it
   `Global_Retain`. Double-click it and paste **SECTION 2**
   (`VAR_GLOBAL RETAIN … END_VAR`) replacing the empty template.
5. Save.

## 4. Wire the call sites

Open `CallSites.st.txt` alongside.

1. **Interlock (Block A):** find the POU containing the lines
   `AxialJogFwd := ControlWord.0;` … `VerticalJogRev := ControlWord.5;`
   (use `Project → Search all` for `AxialJogFwd :=` — it is the main
   PLC_PRG section that also calls the three stepper programs). Replace
   those six lines with Block A (adds `AND NOT Homing?.xBusy`). Leave the
   BasicReset and StatusWord lines untouched.
2. **X axis (Block B1):** POUs tab → double-click `AxialStepperProgram`.
   - Add the three locals from the commented VAR block (tonQuietX,
     xStartReqX, wStatX) to the declaration pane, before its `END_VAR`.
   - Paste the B1 body code at the **end** of the program body (after the
     existing SFC/jog logic — the FB must write the Motor fields last in
     the scan).
3. **Y axis (Block B2):** same procedure in `LateralStepperProgram`.
4. **Z axis (Block B3):** same procedure in `VerticalStepperProgram`.
   Note the extra rotary comment block: the `xDatumPulse` hook must be
   wired to the existing wrap-reconstruction code (variable names to be
   confirmed on the rig).
5. Save.

## 5. Build

1. `Project → Rebuild All` (**F11**).
2. Fix any errors listed in the message window (double-click a message to
   jump to the line). Expected first-build issues: the `VERIFY ON RIG`
   SetPosition pin names, or a missed local declaration.
3. Repeat F11 until **0 errors**.

## 6. Download and run

1. `Online → Communication Parameters…` → channel to the coupler:
   **TCP/IP**, address **192.168.1.21**, port **2455** (the existing
   project channel should already be set up — reuse it).
2. `Online → Login` (**Alt+F8**). Accept the download prompt ("The program
   has changed! Download the new program?" → **Yes**).
3. **Do not press Run yet** if the rig is not physically supervised —
   see `COMMISSIONING.md` first. When ready: `Online → Run` (**F5**).
4. Make it permanent: `Online → Create boot project` (writes the program
   to the coupler's flash so it survives power cycles). Note: homing still
   never auto-starts at boot — the FB only reacts to the START bit.

## 7. Watching the FB live

Two ways, both while logged in (`Online → Login`):

**a) Declaration-window monitoring**
- POUs tab → double-click `AxialStepperProgram` (or PLC_PRG). Since
  `HomingX` is a global FB instance, open it via **Resources →
  Global_Variables** — logged in, every variable shows its live value.
- To see *inside* the FB: POUs tab → double-click `FB_AxisHoming`; CoDeSys
  asks which **instance** to display — pick `HomingX` (or Y/Z). The
  declaration pane now shows live `iState`, `iPhase`, timer states, etc.

**b) Watch list**
- Resources tab → **Watch- and Recipe Manager** → right-click → new watch
  list. Enter expressions one per line, e.g.:
  ```
  HomingX.bState
  HomingX.wFaultCode
  HomingX.iPhase
  AxialHomeCmd
  AxialHomeStatus
  AxialMotor.BasicMailboxActive
  AxialMotor.BasicActualPosition
  I_AxialLimit
  ```
- `Extras → Active` (or the toolbar eye icon) starts monitoring.
- Values can be forced from here for bench tests (`Online → Write values`,
  Ctrl+F7) — e.g. setting `AxialHomeCmd` bit0 without the Modbus host.

## 8. Retain sanity check

After the first successful home: `Online → Logout`, power-cycle the
coupler, log back in and confirm `xAxialHomedR` (Resources →
Global_Retain) is still TRUE and HomeStatus bit8 is set. If it cleared,
the target's retain memory is not configured — check
`Resources → Target Settings` and the coupler's NOVRAM allocation
(VERIFY ON RIG).
