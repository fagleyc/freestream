# AC_Delta_DeviceDriver_Dual.vi — Technical Reference

## Overview

`AC_Delta_DeviceDriver_Dual.vi` is a LabVIEW Virtual Instrument (built in LabVIEW 25.3.2) that controls two Delta ASDA series servo drives over Modbus TCP. It is designed for dual-axis positioning applications such as wind tunnel model actuation (e.g., angle-of-attack and sideslip), where two independent servo axes must be coordinated through a single unified interface.

The VI follows the standard NI **Queued Message Handler (QMH)** design pattern — a producer/consumer architecture where commands are enqueued by the user interface (or calling VI) and consumed by a dispatcher loop that routes each command to the appropriate handler case.

---

## LabVIEW Architecture

### Design Pattern: Queued Message Handler

The QMH consists of three main structures:

1. **Command Queue** — A LabVIEW queue carrying elements of the `DevDriveCMNDS.ctl` typedef enum. The queue decouples the command source (front panel buttons, calling VI, or event structure) from the execution logic, allowing commands to be buffered and processed sequentially.

2. **Consumer While Loop** — The main execution loop. On each iteration it calls `Remove Queue Element.vi` (blocking dequeue with timeout), then feeds the result into a case structure that dispatches to the appropriate sub-VI.

3. **Front Panel / Controls** — Boolean switches (DMC-style round and switch types) for axis enable/disable, plus numeric controls for IP configuration, motion parameters, and position readback indicators.

### Queue Operations

| LabVIEW VI | Role |
|---|---|
| `Create Queue.vi` | Initializes the command queue at startup (element type: `DevDriveCMNDS.ctl`) |
| `Insert Queue Element.vi` | Enqueues a command from the producer side (UI event or external call) |
| `Remove Queue Element.vi` | Dequeues the next command in the consumer loop (blocking with timeout) |

### Modbus Communication

The VI uses the **NI Modbus Master** library (`Modbus Master.lvclass`) to communicate with the Delta drives over TCP/IP:

| LabVIEW VI | Role |
|---|---|
| `Modbus Master.lvclass → Create Master Instance (TCP).vi` | Opens a Modbus TCP connection to a drive at a specified IP address and port (standard port 502) |
| `Modbus Master.lvclass → Close.vi` | Closes the Modbus TCP connection and releases resources |

Each axis has its own independent Modbus TCP connection. The "Dual" in the VI name refers to two simultaneous master instances — one per servo drive.

---

## Command Enum: DevDriveCMNDS.ctl

`DevDriveCMNDS.ctl` is a LabVIEW typedef strict enum that defines the set of commands the QMH can process. It is referenced twice in the VI (once in the front panel type descriptor and once in the block diagram type descriptor), confirming it is a shared typedef control used as the queue element type.

| Command | Sub-VI Called | Description |
|---|---|---|
| **Initialize** | `Create Master Instance (TCP).vi` + enable sequence | Connects to both drives over Modbus TCP, clears any existing faults, transitions each drive through the CIA 402 state machine to Operation Enabled, and writes default motion profile parameters |
| **ReadStatus** | `ReadStatus.vi` | Reads the CIA 402 Statusword (register 0x6041) and error code from each drive, updating fault flags, enable state, and target-reached indicators |
| **ReadEncoder** | `ReadEncoder.vi` | Reads the 32-bit actual position from each drive's encoder feedback registers (0x6064–0x6065) and the actual velocity (0x606C–0x606D) |
| **CalcDegs_fromCal** | `CalcDegs_fromCal.vi` | Converts raw encoder counts to degrees using the stored calibration offset and the encoder/gear configuration: `degrees = (raw_counts − cal_offset) / (counts_per_rev × gear_ratio / 360)` |
| **UpdateMotion** | `UpdateMotion.vi` | Writes profile velocity, acceleration, and deceleration parameters to the drive's motion profile registers (0x6081–0x6086) |
| **PerformCalibration** | `PerformCalibration.vi` | Captures the current encoder position as the zero-reference calibration offset, so subsequent `CalcDegs_fromCal` calls return 0.0° at the current physical position |
| **PerformMove** | `PerformMove.vi` | Converts a target angle (degrees) to encoder counts, writes the target position registers (0x607A–0x607B), sets Profile Position mode (0x6060 = 1), and triggers the move via the Controlword's New Set Point bit |
| **Stop** | *(inline)* | Writes the Halt bit into the Controlword to immediately stop motion |
| **Shutdown** | `Close.vi` + disable sequence | Disables both drives (Controlword → Disable Voltage), closes both Modbus TCP connections, and signals the QMH loop to exit |

---

## Sub-VI Descriptions

### ReadStatus.vi

**Location:** `.\Functions\ReadStatus.vi`

Reads a single holding register at the CIA 402 Statusword address (0x6041) from the target drive. Parses the following bit fields:

| Bit | Name | Meaning |
|---|---|---|
| 0 | Ready to Switch On | Drive is in "Ready to Switch On" state |
| 1 | Switched On | Drive is in "Switched On" state |
| 2 | Operation Enabled | Drive is actively servoing — motion commands accepted |
| 3 | Fault | A drive fault is active; must be cleared before re-enabling |
| 4 | Voltage Enabled | Bus voltage is applied |
| 10 | Target Reached | The commanded profile position move has completed |
| 12 | Homing Attained | The homing sequence completed successfully |

If Bit 3 (Fault) is set, the sub-VI also reads the Error Code register (0x603F) to identify the specific alarm.

### ReadEncoder.vi

**Location:** `.\Functions\ReadEncoder.vi`

Reads two consecutive holding registers starting at 0x6064 (Actual Position) and combines them into a signed 32-bit integer representing the raw encoder count. Also reads two registers at 0x606C (Actual Velocity) for real-time speed feedback.

The 32-bit assembly is: `value = (register_hi << 16) | register_lo`, with sign extension for values ≥ 0x80000000.

### CalcDegs_fromCal.vi

**Location:** `.\Functions\CalcDegs_fromCal.vi`

Performs the unit conversion from raw encoder counts to mechanical degrees:

```
counts_per_degree = encoder_counts_per_rev × gear_ratio / 360.0
position_degrees  = (raw_encoder_counts − calibration_offset_counts) / counts_per_degree
```

The calibration offset is set by `PerformCalibration.vi` and represents the encoder count at the defined zero position. This allows the system to report angles relative to a known physical reference (e.g., model zero-alpha).

### UpdateMotion.vi

**Location:** `.\Functions\UpdateMotion.vi`

Writes three pairs of 32-bit motion profile parameters to the drive:

| Parameter | Registers | Units |
|---|---|---|
| Profile Velocity | 0x6081–0x6082 | 0.1 RPM (Delta convention) |
| Profile Acceleration | 0x6083–0x6084 | milliseconds (time to reach profile velocity) |
| Profile Deceleration | 0x6085–0x6086 | milliseconds (time to stop from profile velocity) |

These parameters govern the trapezoidal (or S-curve, depending on drive configuration) motion profile used in Profile Position mode.

### PerformCalibration.vi

**Location:** `.\Functions\PerformCalibration.vi`

Reads the current encoder position via `ReadEncoder.vi` and stores it as the calibration offset. After calibration, `CalcDegs_fromCal.vi` will return 0.0° for the current physical position. This is a software-only zero — it does not command any drive motion or modify drive parameters.

### PerformMove.vi

**Location:** `.\Functions\PerformMove.vi`

Executes a profile position move in the following sequence:

1. Set mode of operation to Profile Position (write 1 to register 0x6060)
2. Convert target degrees to encoder counts: `target_counts = target_deg × counts_per_degree + cal_offset`
3. Write target position to registers 0x607A (low) and 0x607B (high)
4. Write Controlword with Enable Operation + New Set Point bits set (0x001F for absolute, 0x005F for relative)
5. After a short delay (~10 ms), clear the New Set Point bit (edge-triggered latch)

The move executes asynchronously — the drive profiles the motion internally. Completion is detected by polling the Target Reached bit (Bit 10) of the Statusword.

---

## CIA 402 State Machine

The Delta ASDA drives implement the CANopen CIA 402 (IEC 61800-7) state machine. The VI manages state transitions through the Controlword register (0x6040):

```
                          ┌──────────────┐
                          │  Not Ready   │
                          │  to Switch On│
                          └──────┬───────┘
                                 │ (automatic)
                          ┌──────▼───────┐
                          │  Switch On   │
                          │  Disabled    │
                          └──────┬───────┘
                                 │ CW = 0x0006 (Shutdown)
                          ┌──────▼───────┐
                          │  Ready to    │
                          │  Switch On   │
                          └──────┬───────┘
                                 │ CW = 0x0007 (Switch On)
                          ┌──────▼───────┐
                          │  Switched On │
                          └──────┬───────┘
                                 │ CW = 0x000F (Enable Operation)
                          ┌──────▼───────┐
                          │  Operation   │
                          │  Enabled     │◄──── Motion commands accepted
                          └──────────────┘
```

The Initialize command walks through this sequence with short delays between transitions. The Shutdown command writes `CW = 0x0000` (Disable Voltage) to return to the Switch On Disabled state.

Fault recovery requires writing `CW = 0x0080` (Fault Reset), waiting for the fault bit to clear, then re-running the enable sequence.

---

## Front Panel Controls

The VI front panel uses DMC-style boolean controls (identified in the VI as `user.DMC_Style` with "Boolean Switch" and "Boolean Round" style names). Based on the dual-axis architecture, the expected front panel layout includes:

| Control / Indicator | Type | Purpose |
|---|---|---|
| Axis A Enable | Boolean Switch | Software enable/disable for axis A |
| Axis B Enable | Boolean Switch | Software enable/disable for axis B |
| Axis A Position (°) | Numeric Indicator | Current angle of axis A from CalcDegs_fromCal |
| Axis B Position (°) | Numeric Indicator | Current angle of axis B from CalcDegs_fromCal |
| Axis A Status | Cluster / LEDs | Fault, enabled, homed, target-reached indicators |
| Axis B Status | Cluster / LEDs | Fault, enabled, homed, target-reached indicators |
| Target Position | Numeric Control | Commanded angle in degrees |
| Command Selector | Enum (DevDriveCMNDS) | Selects which command to enqueue |

The font styling uses Segoe UI and Segoe UI Semibold (identified in the VI's font table).

---

## Modbus TCP Connection Details

| Parameter | Typical Value |
|---|---|
| Protocol | Modbus TCP (function codes 0x03 Read Holding, 0x06 Write Single, 0x10 Write Multiple) |
| Port | 502 (standard Modbus TCP) |
| Unit ID | 1 (configurable per axis) |
| Timeout | 3 seconds |
| Byte Order | Big-endian (Modbus standard), 32-bit values split across two consecutive 16-bit registers (low word first for Delta ASDA) |

Each axis connects to a separate IP address. For a typical bench setup:

- Axis A: 192.168.1.1:502
- Axis B: 192.168.1.2:502

---

## Python Translation

The Python equivalent (`ac_delta_device_driver_dual.py`) preserves the QMH architecture using:

| LabVIEW Concept | Python Equivalent |
|---|---|
| LabVIEW Queue | `queue.Queue` from the standard library |
| Consumer While Loop | `threading.Thread` running `_worker_loop()` |
| Case Structure (DevDriveCMNDS) | `_dispatch()` method with if/elif chain on `DevDriveCMNDS` enum |
| Modbus Master.lvclass | `pymodbus.client.ModbusTcpClient` |
| Sub-VIs (ReadStatus, etc.) | Methods on the `DeltaServoAxis` class |
| Front Panel Booleans | Instance attributes (`axis_a_enabled`, `axis_b_enabled`) |
| Parallel status polling | Separate `_poll_loop()` thread |

The single external dependency is `pymodbus` (`pip install pymodbus`).

---

## File Inventory

| File | Source | Description |
|---|---|---|
| `AC_Delta_DeviceDriver_Dual.vi` | LabVIEW 25.3.2 | Top-level QMH driver VI |
| `DevDriveCMNDS.ctl` | LabVIEW typedef | Command enum (strict typedef) |
| `Functions/ReadStatus.vi` | Sub-VI | Read CIA 402 statusword |
| `Functions/ReadEncoder.vi` | Sub-VI | Read 32-bit encoder position |
| `Functions/CalcDegs_fromCal.vi` | Sub-VI | Encoder counts → degrees conversion |
| `Functions/UpdateMotion.vi` | Sub-VI | Write motion profile parameters |
| `Functions/PerformCalibration.vi` | Sub-VI | Set encoder zero reference |
| `Functions/PerformMove.vi` | Sub-VI | Command a profile position move |
| `ac_delta_device_driver_dual.py` | Python translation | Complete Python equivalent |

---

## Notes

- The VI references the NI Modbus library from `<vilib>\Modbus\master\`, which is the standard NI LabVIEW Modbus API (not a third-party toolkit).
- The register map assumes Delta ASDA drives using the CANopen-over-Modbus object dictionary mapping. If the drives are configured to use Delta's native P-parameter addressing instead, the register constants in both the LabVIEW sub-VIs and the Python `DeltaReg` class will need to be adjusted.
- The "Dual" architecture is two independent single-axis controllers sharing a command queue — there is no interpolated multi-axis coordination. Moves are dispatched to each axis sequentially within the same command handler call.
