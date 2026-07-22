# AC Delta Device Driver — Sub-VI Implementation Reference

## Purpose

This document describes the internal workings of every sub-VI in the AC Delta dual-axis servo driver, extracted from the LabVIEW binary `.vi` files. Use it to implement or verify the Python test environment against the actual LabVIEW behavior.

---

## Call Graph

```
AC_Delta_DeviceDriver_Dual.vi  (top-level QMH)
│
├── Create Queue.vi / Insert Queue Element.vi / Remove Queue Element.vi
├── Modbus Master: Create Master Instance (TCP).vi
├── Modbus Master: Close.vi
│
├── ReadStatus.vi
│   ├── Modbus Master: Read Holding Registers.vi
│   └── StatusWord.vi                              ← bit-field parser
│
├── ReadEncoder.vi
│   └── Modbus Master: Read Holding Registers.vi
│
├── CalcDegs_fromCal.vi                            ← pure math (counts → degrees)
│
├── UpdateMotion.vi
│   ├── Modbus Master: Write Single Holding Register.vi
│   └── CntrlWord.vi                              ← writes CIA 402 Controlword
│
├── PerformCalibration.vi
│   ├── Prompt User for Input (Express VI) × 2     ← lower + upper limit dialogs
│   └── ReadEncoder.vi
│
├── PerformMove.vi
│   ├── CalcCts_fromCal.vi                         ← degrees → counts
│   ├── ReadEncoder.vi
│   ├── ReadStatus.vi
│   ├── CalcDegs_fromCal.vi
│   ├── SetSpeedTable.vi                           ← speed profile selection
│   ├── UpdateMotion.vi
│   └── Change Detector.vi                         ← NI edge-detect utility
│
└── AddACtoMeasurementCluster.vi
    ├── Measurement Data DevCh.ctl                 ← channel data typedef
    └── Measurement Info.ctl                       ← metadata typedef
```

---

## 1. ReadStatus.vi

### What It Does

Reads the drive's status over Modbus and parses it into individual flags.

### Modbus Calls

| Call | Function | Register | Count | Purpose |
|---|---|---|---|---|
| 1 | Read Holding Registers (FC 0x03) | Status word register | 1 | Raw 16-bit status word |

The exact register address depends on whether the Delta is using CIA 402 mapping (`0x6041`) or Delta native P-parameter mapping. The VI reads a single register, then passes the raw U16 value to `StatusWord.vi` for bit parsing.

### Data Flow

```
Modbus Master Ref ──→ Read Holding Registers.vi ──→ raw U16
                                                       │
                                                       ▼
                                                  StatusWord.vi
                                                       │
                                                       ▼
                                              Status cluster out
                                              (individual booleans)
```

### Python Equivalent

```python
def read_status(client, unit_id, status_register):
    """Read and parse the drive status word."""
    result = client.read_holding_registers(status_register, 1, slave=unit_id)
    raw = result.registers[0]
    return parse_status_word(raw)
```

---

## 2. StatusWord.vi

### What It Does

Pure bit-field parser — takes a raw U16 status word and extracts individual boolean flags. No Modbus calls. Contains a "verify" string suggesting it validates the status word against expected patterns (e.g., verifying that the CIA 402 state machine is in a consistent state).

### CIA 402 Status Word Bit Map

```
Bit 15 ┌──────────────────────────────────────────────┐ Bit 0
       │ 15│14│13│12│11│10│ 9│ 8│ 7│ 6│ 5│ 4│ 3│ 2│ 1│ 0│
       └──────────────────────────────────────────────┘
        │       │     │        │  │  │  │  │  │  │  │  └─ Ready to Switch On
        │       │     │        │  │  │  │  │  │  │  └──── Switched On
        │       │     │        │  │  │  │  │  │  └─────── Operation Enabled
        │       │     │        │  │  │  │  │  └────────── Fault
        │       │     │        │  │  │  │  └───────────── Voltage Enabled
        │       │     │        │  │  │  └──────────────── Quick Stop Active (0 = active)
        │       │     │        │  │  └─────────────────── Switch On Disabled
        │       │     │        │  └────────────────────── Warning
        │       │     │        └───────────────────────── (mode-specific / manufacturer)
        │       │     └────────────────────────────────── Target Reached
        │       └──────────────────────────────────────── Homing Attained / ref. reached
        └──────────────────────────────────────────────── (manufacturer-specific)
```

### CIA 402 State Decode (mask 0x006F)

The lower bits encode the drive state via a specific mask pattern:

| Statusword & 0x006F | State |
|---|---|
| `0x0000` | Not Ready to Switch On |
| `0x0040` | Switch On Disabled |
| `0x0021` | Ready to Switch On |
| `0x0023` | Switched On |
| `0x0027` | Operation Enabled |
| `0x0007` | Quick Stop Active |
| `0x000F` or `0x002F` | Fault Reaction Active |
| `0x0008` | Fault |

### Python Equivalent

```python
def parse_status_word(raw: int) -> dict:
    """Parse CIA 402 status word into named flags."""
    return {
        "ready_to_switch_on":  bool(raw & 0x0001),
        "switched_on":         bool(raw & 0x0002),
        "operation_enabled":   bool(raw & 0x0004),
        "fault":               bool(raw & 0x0008),
        "voltage_enabled":     bool(raw & 0x0010),
        "quick_stop_active":   not bool(raw & 0x0020),  # inverted logic
        "switch_on_disabled":  bool(raw & 0x0040),
        "warning":             bool(raw & 0x0080),
        "target_reached":      bool(raw & 0x0400),
        "homing_attained":     bool(raw & 0x1000),
        "raw": raw,
    }

def get_cia402_state(raw: int) -> str:
    """Decode the CIA 402 state machine state."""
    masked = raw & 0x006F
    states = {
        0x0000: "Not Ready to Switch On",
        0x0040: "Switch On Disabled",
        0x0021: "Ready to Switch On",
        0x0023: "Switched On",
        0x0027: "Operation Enabled",
        0x0007: "Quick Stop Active",
        0x000F: "Fault Reaction Active",
        0x002F: "Fault Reaction Active",
        0x0008: "Fault",
    }
    return states.get(masked, f"Unknown (0x{masked:04X})")
```

---

## 3. ReadEncoder.vi

### What It Does

Reads the actual position from the drive's encoder feedback registers over Modbus. Returns a 32-bit signed encoder count.

### Modbus Calls

| Call | Function | Register | Count | Purpose |
|---|---|---|---|---|
| 1 | Read Holding Registers (FC 0x03) | Position register(s) | 2 | 32-bit actual position (low + high word) |

The VI reads two consecutive 16-bit registers and assembles them into a signed 32-bit integer.

### 32-Bit Assembly

LabVIEW's NI Modbus library returns registers as an array of U16. The VI combines them:

```
encoder_counts = (register_high << 16) | register_low
if encoder_counts >= 0x80000000:
    encoder_counts -= 0x100000000    # sign extension
```

### Python Equivalent

```python
def read_encoder(client, unit_id, position_register):
    """
    Read 32-bit encoder position from two consecutive Modbus registers.
    
    Args:
        position_register: address of the low word (high word = address + 1)
    
    Returns:
        Signed 32-bit encoder count
    """
    result = client.read_holding_registers(position_register, 2, slave=unit_id)
    lo = result.registers[0]
    hi = result.registers[1]
    
    value = (hi << 16) | lo
    if value >= 0x80000000:
        value -= 0x100000000
    return value
```

---

## 4. CalcDegs_fromCal.vi

### What It Does

Converts raw encoder counts to degrees using a **two-point linear calibration**. This is NOT a simple offset — it uses two known reference positions (lower limit and upper limit) to compute a linear mapping from counts to degrees. This accounts for both offset AND scale (gear ratio, encoder resolution, any mechanical slop).

Has a **Boolean Round** control — likely used to handle angular wrap-around (e.g., wrapping 359° → 0°) or rounding the output to a fixed precision.

### No Modbus Calls

Pure math function — takes encoder counts and calibration data as inputs.

### Two-Point Calibration Math

The calibration stores:
- `lower_limit_deg`: known angle at position 1 (from control panel readout)
- `lower_limit_cts`: encoder count at position 1
- `upper_limit_deg`: known angle at position 2 (from control panel readout)  
- `upper_limit_cts`: encoder count at position 2

The conversion is:

```
counts_per_degree = (upper_limit_cts - lower_limit_cts) / (upper_limit_deg - lower_limit_deg)

degrees = lower_limit_deg + (raw_counts - lower_limit_cts) / counts_per_degree
```

Or equivalently:

```
degrees = lower_limit_deg + (raw_counts - lower_limit_cts) * (upper_limit_deg - lower_limit_deg)
                                                            / (upper_limit_cts - lower_limit_cts)
```

### Python Equivalent

```python
@dataclass
class TwoPointCal:
    """Two-point calibration data from PerformCalibration."""
    lower_limit_deg: float = 0.0
    lower_limit_cts: int = 0
    upper_limit_deg: float = 0.0
    upper_limit_cts: int = 0
    
    @property
    def counts_per_degree(self) -> float:
        delta_deg = self.upper_limit_deg - self.lower_limit_deg
        delta_cts = self.upper_limit_cts - self.lower_limit_cts
        if delta_deg == 0:
            return 0.0
        return delta_cts / delta_deg


def calc_degs_from_cal(raw_counts: int, cal: TwoPointCal, 
                        wrap_360: bool = False) -> float:
    """
    Convert encoder counts to degrees using two-point calibration.
    
    Args:
        raw_counts: current encoder reading
        cal: two-point calibration data
        wrap_360: if True, wrap result to [0, 360) range
    """
    if cal.counts_per_degree == 0:
        return 0.0
    
    degrees = (cal.lower_limit_deg 
               + (raw_counts - cal.lower_limit_cts) / cal.counts_per_degree)
    
    if wrap_360:
        degrees = degrees % 360.0
    
    return degrees
```

---

## 5. CalcCts_fromCal.vi

### What It Does

The **inverse** of `CalcDegs_fromCal.vi` — converts a target angle in degrees back to encoder counts using the same two-point calibration. Used by `PerformMove.vi` to compute the target encoder position for a commanded angle.

Also has a **Boolean Round** control (likely rounds to nearest integer count).

### No Modbus Calls

Pure math function.

### Math

```
target_counts = lower_limit_cts + (target_deg - lower_limit_deg) * counts_per_degree
```

### Python Equivalent

```python
def calc_cts_from_cal(target_deg: float, cal: TwoPointCal,
                       round_result: bool = True) -> int:
    """
    Convert target degrees to encoder counts using two-point calibration.
    
    This is the inverse of calc_degs_from_cal.
    """
    counts = (cal.lower_limit_cts 
              + (target_deg - cal.lower_limit_deg) * cal.counts_per_degree)
    
    if round_result:
        return round(counts)
    return int(counts)
```

---

## 6. PerformCalibration.vi

### What It Does

Executes a **two-point manual calibration** procedure with operator interaction. This is the largest sub-VI (~70 KB) and contains two instances of the NI "Prompt User for Input" Express VI, plus a call to `ReadEncoder.vi`.

### Procedure (Operator-Guided)

**Step 1 — Lower Limit:**
1. Displays dialog: *"Manually move sting to lower limit and enter value from control panel"*
2. Operator physically jogs the model/sting to the lower angular limit
3. Operator reads the angle from the facility's reference (e.g., inclinometer, protractor on the sting support)
4. Operator enters the value in the dialog's **"Number: Lower Limit"** input field
5. VI calls `ReadEncoder.vi` to capture the encoder count at this position
6. Stores: `lower_limit_deg` and `lower_limit_cts`

**Step 2 — Upper Limit:**
1. Displays dialog: *"Manually move sting to upper limit and enter value from control panel"*
2. Operator jogs the model/sting to the upper angular limit
3. Operator reads and enters the angle in the **"Number: Upper Limit"** input field
4. VI calls `ReadEncoder.vi` again to capture the encoder count
5. Stores: `upper_limit_deg` and `upper_limit_cts`

**Result:**
The calibration produces a linear mapping between encoder counts and degrees. Both `CalcDegs_fromCal.vi` and `CalcCts_fromCal.vi` use this mapping.

### Sub-VI Calls

| Sub-VI | When Called | Purpose |
|---|---|---|
| `Prompt User for Input` (Express) | Step 1 | Get lower limit angle from operator |
| `ReadEncoder.vi` | After Step 1 confirm | Capture lower encoder count |
| `Prompt User for Input` (Express) | Step 2 | Get upper limit angle from operator |
| `ReadEncoder.vi` | After Step 2 confirm | Capture upper encoder count |

### Python Equivalent

```python
def perform_calibration(client, unit_id, position_register) -> TwoPointCal:
    """
    Interactive two-point calibration.
    
    In a GUI environment, replace input() with dialog boxes.
    In an automated test, provide known positions programmatically.
    """
    cal = TwoPointCal()
    
    # Step 1: Lower limit
    print("Manually move sting to LOWER limit position.")
    lower_deg_str = input("Enter lower limit angle (degrees) from control panel: ")
    cal.lower_limit_deg = float(lower_deg_str)
    cal.lower_limit_cts = read_encoder(client, unit_id, position_register)
    print(f"  Captured: {cal.lower_limit_deg}° = {cal.lower_limit_cts} counts")
    
    # Step 2: Upper limit
    print("Manually move sting to UPPER limit position.")
    upper_deg_str = input("Enter upper limit angle (degrees) from control panel: ")
    cal.upper_limit_deg = float(upper_deg_str)
    cal.upper_limit_cts = read_encoder(client, unit_id, position_register)
    print(f"  Captured: {cal.upper_limit_deg}° = {cal.upper_limit_cts} counts")
    
    # Validate
    if cal.counts_per_degree == 0:
        raise ValueError("Calibration failed: zero counts_per_degree "
                         "(did the encoder move between the two points?)")
    
    print(f"  Scale: {cal.counts_per_degree:.2f} counts/degree")
    return cal
```

---

## 7. UpdateMotion.vi

### What It Does

Writes motion profile parameters to the drive and optionally triggers motion via the Controlword. Uses `Write Single Holding Register.vi` for individual register writes and calls `CntrlWord.vi` to write the CIA 402 Controlword.

Has a **Boolean Switch** — likely the "go/enable" signal that determines whether to actually write the Controlword (i.e., trigger the move) or just update the parameters.

### Modbus Calls

| Call | Function | Register | Purpose |
|---|---|---|---|
| 1+ | Write Single Holding Register (FC 0x06) | Profile parameter registers | Velocity, acceleration, deceleration |
| Last | Write Single Holding Register via `CntrlWord.vi` | Controlword register | Trigger motion or change state |

### Sequence

1. Write profile velocity to the drive
2. Write profile acceleration
3. Write profile deceleration
4. If Boolean Switch is ON → call `CntrlWord.vi` to write the Controlword (e.g., `0x001F` = Enable + New Set Point for absolute move)

### Python Equivalent

```python
def update_motion(client, unit_id, velocity_reg, accel_reg, decel_reg,
                  controlword_reg, velocity, accel, decel,
                  trigger_move=False, controlword_value=0x000F):
    """
    Write motion profile parameters and optionally trigger via controlword.
    """
    # Write profile velocity (single register for 16-bit, or two for 32-bit)
    client.write_register(velocity_reg, velocity, slave=unit_id)
    
    # Write acceleration
    client.write_register(accel_reg, accel, slave=unit_id)
    
    # Write deceleration
    client.write_register(decel_reg, decel, slave=unit_id)
    
    # Optionally trigger
    if trigger_move:
        write_controlword(client, unit_id, controlword_reg, controlword_value)
```

---

## 8. CntrlWord.vi (referenced by UpdateMotion.vi)

### What It Does

Writes the CIA 402 Controlword register. This is a thin wrapper around `Write Single Holding Register` that targets the Controlword specifically.

### Controlword Bit Map

```
Bit 15 ┌──────────────────────────────────────────────┐ Bit 0
       │ 15│14│13│12│11│10│ 9│ 8│ 7│ 6│ 5│ 4│ 3│ 2│ 1│ 0│
       └──────────────────────────────────────────────┘
                               │  │  │  │  │  │  │  │  └─ Switch On
                               │  │  │  │  │  │  │  └──── Enable Voltage
                               │  │  │  │  │  │  └─────── Quick Stop (1 = enabled)
                               │  │  │  │  │  └────────── Enable Operation
                               │  │  │  │  └───────────── New Set Point (pos mode)
                               │  │  │  └──────────────── Change Set Immediately
                               │  │  └─────────────────── Abs/Rel (0=abs, 1=rel)
                               │  └────────────────────── Fault Reset (rising edge)
                               └───────────────────────── Halt
```

### Common Controlword Values

| Value | Action | Description |
|---|---|---|
| `0x0006` | Shutdown | Ready to Switch On |
| `0x0007` | Switch On | Switched On |
| `0x000F` | Enable Operation | Servo active, no motion |
| `0x001F` | Absolute Move | Enable + New Set Point (absolute) |
| `0x005F` | Relative Move | Enable + New Set Point (relative) |
| `0x010F` | Halt | Stop motion, keep enabled |
| `0x0080` | Fault Reset | Clear fault (rising edge) |
| `0x0000` | Disable Voltage | Power off |

---

## 9. SetSpeedTable.vi

### What It Does

Selects and configures the speed profile for a move. Contains a **"wheel_caster"** identifier, suggesting it handles different speed profiles for different axis types or motion modes (the dual axes may have physically different mechanisms or travel ranges that require different speed ramps).

Has a **Boolean Switch** — likely selects between two speed profiles or enables/disables the speed table lookup.

### No Direct Modbus Calls

Pure computation — looks up or calculates velocity, acceleration, and deceleration values from a table or set of rules, then passes them to `UpdateMotion.vi`.

### Likely Behavior

The "speed table" pattern is common in wind tunnel positioning systems. The VI probably implements one of these approaches:

**Approach A — Distance-Based Speed Selection:**
Select speed/accel based on how far the axis needs to travel:
- Large moves (>10°) → full speed
- Medium moves (1–10°) → reduced speed
- Small moves (<1°) → creep speed

**Approach B — Axis-Type Selection:**
The "wheel_caster" name suggests different speed profiles per axis type. In a sting-mounted model positioner, one axis might be the main alpha sweep (long travel, higher speed) while the other is a fine-adjustment or roll axis with different dynamics.

### Python Equivalent

```python
@dataclass
class SpeedProfile:
    """Motion speed profile."""
    velocity: int = 1000      # RPM or drive units
    acceleration: int = 500   # ms or drive units
    deceleration: int = 500   # ms or drive units


def set_speed_table(distance_deg: float, axis_type: str = "default",
                    use_table: bool = True) -> SpeedProfile:
    """
    Select speed profile based on move distance and axis type.
    
    Args:
        distance_deg: absolute distance of the move in degrees
        axis_type: "wheel_caster" or other axis identifier
        use_table: if False, return default profile
    """
    if not use_table:
        return SpeedProfile()
    
    abs_dist = abs(distance_deg)
    
    # Distance-based speed selection (adjust thresholds for your system)
    if abs_dist > 10.0:
        return SpeedProfile(velocity=3000, acceleration=300, deceleration=300)
    elif abs_dist > 2.0:
        return SpeedProfile(velocity=1500, acceleration=400, deceleration=400)
    elif abs_dist > 0.5:
        return SpeedProfile(velocity=500, acceleration=500, deceleration=500)
    else:
        return SpeedProfile(velocity=100, acceleration=800, deceleration=800)
```

---

## 10. PerformMove.vi

### What It Does

The main motion orchestration VI. This is the most complex sub-VI and calls nearly every other function in the driver. It converts a target angle to encoder counts, selects the speed profile, writes the motion parameters, triggers the move, and monitors completion.

Contains the string **"MOVE"** — likely a state label within an internal state machine or case structure.

### Sub-VI Call Sequence

The move executes in this order (reconstructed from the VI's sub-VI reference list and block diagram structure):

```
PerformMove.vi
│
│  ┌─ SETUP PHASE ─────────────────────────────┐
│  │                                            │
│  ├── CalcCts_fromCal.vi                       │  Convert target degrees → encoder counts
│  │     (target_deg, calibration) → target_cts │
│  │                                            │
│  ├── ReadEncoder.vi                           │  Get current position
│  │     → current_cts                          │
│  │                                            │
│  ├── SetSpeedTable.vi                         │  Select speed profile based on
│  │     (target_cts - current_cts) → profile   │  move distance
│  │                                            │
│  └── ReadStatus.vi                            │  Verify drive is enabled, no fault
│        → status flags                         │
│                                               │
│  ┌─ EXECUTE PHASE ───────────────────────────┐
│  │                                            │
│  ├── UpdateMotion.vi                          │  Write velocity/accel/decel +
│  │     (profile, trigger=True)                │  write Controlword to start move
│  │                                            │
│  └── Change Detector.vi                       │  Edge detect on "new setpoint"
│        (NI utility for one-shot trigger)      │
│                                               │
│  ┌─ MONITOR PHASE ───────────────────────────┐
│  │                                            │
│  ├── ReadStatus.vi (loop)                     │  Poll until Target Reached
│  ├── ReadEncoder.vi (loop)                    │  Update position display
│  └── CalcDegs_fromCal.vi (loop)              │  Convert to degrees for display
│                                               │
│  └── Return final position                    │
```

### Python Equivalent

```python
def perform_move(client, unit_id, target_deg: float, cal: TwoPointCal,
                 registers: dict, timeout_s: float = 30.0,
                 use_speed_table: bool = True) -> float:
    """
    Execute a position move and wait for completion.
    
    Args:
        client: Modbus TCP client
        unit_id: Modbus slave ID
        target_deg: target angle in degrees
        cal: two-point calibration data
        registers: dict of register addresses
        timeout_s: move timeout
        use_speed_table: use distance-based speed selection
    
    Returns:
        Final position in degrees
    """
    # --- SETUP ---
    
    # Convert target to counts
    target_cts = calc_cts_from_cal(target_deg, cal)
    
    # Read current position
    current_cts = read_encoder(client, unit_id, registers['position'])
    move_distance_cts = target_cts - current_cts
    
    # Check status before moving
    status = read_status(client, unit_id, registers['status'])
    if status['fault']:
        raise RuntimeError(f"Drive in fault state: SW=0x{status['raw']:04X}")
    if not status['operation_enabled']:
        raise RuntimeError("Drive not enabled")
    
    # Select speed profile
    move_distance_deg = move_distance_cts / cal.counts_per_degree
    profile = set_speed_table(move_distance_deg) if use_speed_table else SpeedProfile()
    
    # --- EXECUTE ---
    
    # Set mode to Profile Position (mode 1)
    client.write_register(registers['mode_of_operation'], 1, slave=unit_id)
    
    # Write target position (32-bit, two registers)
    lo = target_cts & 0xFFFF
    hi = (target_cts >> 16) & 0xFFFF
    if target_cts < 0:
        unsigned = target_cts + 0x100000000
        lo = unsigned & 0xFFFF
        hi = (unsigned >> 16) & 0xFFFF
    client.write_registers(registers['target_position'], [lo, hi], slave=unit_id)
    
    # Write motion profile
    client.write_register(registers['profile_velocity'], profile.velocity, slave=unit_id)
    client.write_register(registers['profile_accel'], profile.acceleration, slave=unit_id)
    client.write_register(registers['profile_decel'], profile.deceleration, slave=unit_id)
    
    # Trigger move: Enable + New Set Point (absolute)
    client.write_register(registers['controlword'], 0x001F, slave=unit_id)
    time.sleep(0.01)
    # Clear New Set Point bit (edge trigger)
    client.write_register(registers['controlword'], 0x000F, slave=unit_id)
    
    # --- MONITOR ---
    
    t0 = time.time()
    while (time.time() - t0) < timeout_s:
        status = read_status(client, unit_id, registers['status'])
        
        if status['fault']:
            raise RuntimeError(f"Fault during move: SW=0x{status['raw']:04X}")
        
        if status['target_reached']:
            break
        
        time.sleep(0.05)  # 50 ms polling
    else:
        raise TimeoutError(f"Move to {target_deg}° timed out after {timeout_s}s")
    
    # Read and return final position
    final_cts = read_encoder(client, unit_id, registers['position'])
    final_deg = calc_degs_from_cal(final_cts, cal)
    
    return final_deg
```

---

## 11. AddACtoMeasurementCluster.vi

### What It Does

Packages the actuator/axis controller data into the wind tunnel's measurement data structure for logging. References two LabVIEW typedef controls from the `FileIO/DLOG` library path:

- **`Measurement Data DevCh.ctl`** — Per-device-channel data cluster. This likely contains fields for the current position (degrees), raw encoder counts, status flags, and axis identifier.

- **`Measurement Info.ctl`** — Metadata cluster with timestamp, run info, configuration, and facility information.

### No Modbus Calls

This is a data packaging function. It takes the current axis state (position, status, encoder) and inserts it into the larger measurement cluster that gets written to the data log (presumably HDF5 or TDMS in the full system).

### Python Equivalent

```python
@dataclass
class MeasurementDataDevCh:
    """Per-channel measurement data — matches Measurement Data DevCh.ctl."""
    position_deg: float = 0.0
    raw_encoder: int = 0
    status_word: int = 0
    enabled: bool = False
    fault: bool = False
    target_reached: bool = False
    timestamp: float = 0.0


def add_ac_to_measurement_cluster(axis_a_state, axis_b_state,
                                   cal_a, cal_b,
                                   measurement_info: dict) -> dict:
    """
    Package both axis states into the measurement cluster.
    
    Returns a dict matching the LabVIEW measurement cluster structure.
    """
    ch_a = MeasurementDataDevCh(
        position_deg=calc_degs_from_cal(axis_a_state.raw_encoder, cal_a),
        raw_encoder=axis_a_state.raw_encoder,
        status_word=axis_a_state.status_word,
        enabled=axis_a_state.is_enabled,
        fault=axis_a_state.is_fault,
        target_reached=axis_a_state.target_reached,
        timestamp=time.time(),
    )
    ch_b = MeasurementDataDevCh(
        position_deg=calc_degs_from_cal(axis_b_state.raw_encoder, cal_b),
        raw_encoder=axis_b_state.raw_encoder,
        status_word=axis_b_state.status_word,
        enabled=axis_b_state.is_enabled,
        fault=axis_b_state.is_fault,
        target_reached=axis_b_state.target_reached,
        timestamp=time.time(),
    )
    
    return {
        "measurement_info": measurement_info,
        "axis_a": ch_a,
        "axis_b": ch_b,
    }
```

---

## Key Correction: Two-Point Calibration

The original Python driver (`ac_delta_device_driver_dual.py`) implemented a **single-point zero-offset** calibration. The actual LabVIEW code uses a **two-point linear calibration** that captures both offset and scale. This is the critical difference to update in the Python test environment:

| | Original Python | Actual LabVIEW |
|---|---|---|
| **Cal points** | 1 (zero offset only) | 2 (lower limit + upper limit) |
| **Stored data** | `cal_offset_counts` | `lower_limit_deg`, `lower_limit_cts`, `upper_limit_deg`, `upper_limit_cts` |
| **Counts→Deg** | `(raw - offset) / (enc_res * gear / 360)` | `lower_deg + (raw - lower_cts) * (Δdeg / Δcts)` |
| **Deg→Counts** | `target * (enc_res * gear / 360) + offset` | `lower_cts + (target - lower_deg) * (Δcts / Δdeg)` |
| **Advantages** | Simpler, needs exact encoder spec | Compensates unknown gear ratio, encoder resolution, mechanical backlash in aggregate |

The two-point approach is more robust for a wind tunnel sting system where the exact mechanical gear ratio may not be precisely known, or where there are reducer stages, belt/chain drives, or other non-ideal transmission elements between the motor and the model.

---

## Register Address Summary

The exact register addresses depend on the Delta drive configuration (CIA 402 mapping vs. native P-parameter mapping). Here are the standard CIA 402 addresses used across the sub-VIs:

| Register | Address | Width | Used By | R/W |
|---|---|---|---|---|
| Controlword | `0x6040` | 16-bit | UpdateMotion, CntrlWord | W |
| Statusword | `0x6041` | 16-bit | ReadStatus → StatusWord | R |
| Mode of Operation | `0x6060` | 8-bit* | PerformMove | W |
| Mode Display | `0x6061` | 8-bit* | (verification) | R |
| Actual Position (lo) | `0x6064` | 16-bit | ReadEncoder | R |
| Actual Position (hi) | `0x6065` | 16-bit | ReadEncoder | R |
| Target Position (lo) | `0x607A` | 16-bit | PerformMove | W |
| Target Position (hi) | `0x607B` | 16-bit | PerformMove | W |
| Profile Velocity (lo) | `0x6081` | 16-bit | UpdateMotion | W |
| Profile Velocity (hi) | `0x6082` | 16-bit | UpdateMotion | W |
| Profile Acceleration (lo) | `0x6083` | 16-bit | UpdateMotion | W |
| Profile Acceleration (hi) | `0x6084` | 16-bit | UpdateMotion | W |
| Profile Deceleration (lo) | `0x6085` | 16-bit | UpdateMotion | W |
| Profile Deceleration (hi) | `0x6086` | 16-bit | UpdateMotion | W |
| Error Code | `0x603F` | 16-bit | ReadStatus (on fault) | R |

*Note: 8-bit objects still occupy a full 16-bit Modbus register; the upper byte is ignored.*

### Delta Native P-Parameter Alternative

If the drives are configured to use Delta's native addressing (not CIA 402 object dictionary), the register mapping changes. Delta P-parameters map to Modbus as: `address = (group × 256) + parameter_number`.

For example:
- P0-02 (display) → `0x0002`
- P1-01 (encoder res) → `0x0101`
- P1-09 (gear numerator) → `0x0109`
- P1-10 (gear denominator) → `0x010A`

Check the drive configuration parameter (typically P3-05 or P3-06 on ASDA-A2/B2) to determine which address scheme is active.

---

## Complete Test Harness

Here is a minimal integration test that exercises the full move pipeline using the functions above:

```python
from pymodbus.client import ModbusTcpClient
import time

# Register map — adjust for your drive configuration
REGS = {
    'controlword':      0x6040,
    'status':           0x6041,
    'mode_of_operation': 0x6060,
    'position':         0x6064,   # lo word (hi = +1)
    'target_position':  0x607A,   # lo word (hi = +1)
    'profile_velocity': 0x6081,
    'profile_accel':    0x6083,
    'profile_decel':    0x6085,
    'error_code':       0x603F,
}

def test_move_pipeline():
    client = ModbusTcpClient(host="192.168.1.1", port=502, timeout=3)
    assert client.connect(), "Connection failed"
    UNIT = 1
    
    try:
        # 1. Read initial status
        status = read_status(client, UNIT, REGS['status'])
        print(f"Initial status: {status}")
        assert not status['fault'], "Drive is in fault"
        
        # 2. Perform two-point calibration
        cal = perform_calibration(client, UNIT, REGS['position'])
        print(f"Calibration: {cal.counts_per_degree:.1f} cts/deg")
        
        # 3. Enable drive (CIA 402 state machine)
        for cw, label in [(0x0006, "Shutdown"), 
                           (0x0007, "SwitchOn"),
                           (0x000F, "EnableOp")]:
            client.write_register(REGS['controlword'], cw, slave=UNIT)
            time.sleep(0.05)
            print(f"  {label}: SW=0x{read_status(client, UNIT, REGS['status'])['raw']:04X}")
        
        # 4. Move to +5 degrees
        final = perform_move(client, UNIT, 5.0, cal, REGS, timeout_s=15)
        print(f"Move complete: {final:.3f}°")
        
        # 5. Verify position
        cts = read_encoder(client, UNIT, REGS['position'])
        deg = calc_degs_from_cal(cts, cal)
        print(f"Verification: {deg:.3f}° ({cts} counts)")
        assert abs(deg - 5.0) < 0.1, f"Position error: {deg - 5.0:.3f}°"
        
    finally:
        # Disable and disconnect
        client.write_register(REGS['controlword'], 0x0000, slave=UNIT)
        client.close()
        print("Test complete")

if __name__ == "__main__":
    test_move_pipeline()
```
