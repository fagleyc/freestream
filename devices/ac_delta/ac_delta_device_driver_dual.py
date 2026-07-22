"""
AC_Delta_DeviceDriver_Dual.py

Python equivalent of AC_Delta_DeviceDriver_Dual.vi (LabVIEW).

This module implements a dual-axis Delta ASDA servo drive controller
communicating over Modbus TCP. It mirrors the LabVIEW Queued Message
Handler (QMH) architecture:

  - A command queue dispatches DevDriveCMNDS to handler functions
  - Modbus TCP master communicates with two Delta ASDA servo drives
  - Sub-VI equivalents: ReadStatus, ReadEncoder, CalcDegs_fromCal,
    UpdateMotion, PerformCalibration, PerformMove

Designed for wind tunnel model positioning (dual-axis: e.g., alpha/beta).

Dependencies:
    pip install pymodbus

Author: Converted from LabVIEW VI (NI LabVIEW 25.3.2)
"""

from __future__ import annotations

import enum
import logging
import math
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from pymodbus.client import ModbusTcpClient
from pymodbus.exceptions import ModbusException

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Delta ASDA-A2 Modbus Register Map (common registers)
# Reference: Delta ASDA-A2 User Manual, Ch. 8  Modbus Communications
# All addresses are 0-indexed holding registers (function code 0x03/0x06/0x10)
# ---------------------------------------------------------------------------
class DeltaReg:
    """Delta ASDA servo drive Modbus register addresses."""

    # --- Status / Control ---
    CONTROL_WORD        = 0x6040   # Controlword (CIA 402)
    STATUS_WORD         = 0x6041   # Statusword  (CIA 402)
    MODES_OF_OPERATION  = 0x6060   # Mode select: 1=Profile Position, 3=Profile Velocity, etc.
    MODES_DISPLAY       = 0x6061   # Actual mode of operation

    # --- Position ---
    TARGET_POSITION_LO  = 0x607A   # Target position low 16 bits
    TARGET_POSITION_HI  = 0x607B   # Target position high 16 bits
    ACTUAL_POSITION_LO  = 0x6064   # Actual position (encoder) low 16 bits
    ACTUAL_POSITION_HI  = 0x6065   # Actual position (encoder) high 16 bits

    # --- Velocity ---
    TARGET_VELOCITY_LO  = 0x60FF   # Target velocity low  (Profile Velocity mode)
    TARGET_VELOCITY_HI  = 0x6100   # Target velocity high
    ACTUAL_VELOCITY_LO  = 0x606C   # Actual velocity low
    ACTUAL_VELOCITY_HI  = 0x606D   # Actual velocity high

    # --- Profile parameters ---
    PROFILE_VELOCITY_LO = 0x6081   # Profile velocity low  (used in Profile Position mode)
    PROFILE_VELOCITY_HI = 0x6082   # Profile velocity high
    PROFILE_ACCEL_LO    = 0x6083   # Profile acceleration low
    PROFILE_ACCEL_HI    = 0x6084   # Profile acceleration high
    PROFILE_DECEL_LO    = 0x6085   # Profile deceleration low
    PROFILE_DECEL_HI    = 0x6086   # Profile deceleration high

    # --- Homing ---
    HOMING_METHOD       = 0x6098   # Homing method
    HOMING_SPEED_LO     = 0x6099   # Homing speed (search) low
    HOMING_SPEED_HI     = 0x609A   # Homing speed (search) high

    # --- Alarm / Error ---
    ERROR_CODE          = 0x603F   # Error code register

    # --- Delta-specific parameter registers (P-group) ---
    # These map P-parameters to Modbus: address = group*256 + param
    # Example: P1-01 = 0x0100 + 0x01 = 0x0101
    ENCODER_RESOLUTION  = 0x0101   # P1-01: encoder pulses/rev (typically 1280000 counts/rev)
    GEAR_RATIO_NUM      = 0x0109   # P1-09: electronic gear numerator
    GEAR_RATIO_DEN      = 0x010A   # P1-10: electronic gear denominator
    SERVO_ON            = 0x0100   # P1-00: bit used for software servo-on


# ---------------------------------------------------------------------------
# CIA 402 Controlword / Statusword bit definitions
# ---------------------------------------------------------------------------
class CW:
    """Controlword (0x6040) bit masks — CIA 402 state machine."""
    SWITCH_ON           = 0x0001  # Bit 0
    ENABLE_VOLTAGE      = 0x0002  # Bit 1
    QUICK_STOP          = 0x0004  # Bit 2
    ENABLE_OPERATION    = 0x0008  # Bit 3
    NEW_SET_POINT       = 0x0010  # Bit 4  (Position mode: trigger move)
    CHANGE_IMMEDIATELY  = 0x0020  # Bit 5
    ABS_REL             = 0x0040  # Bit 6  (0 = absolute, 1 = relative)
    FAULT_RESET         = 0x0080  # Bit 7
    HALT                = 0x0100  # Bit 8

    # Common composite commands
    SHUTDOWN            = QUICK_STOP | ENABLE_VOLTAGE             # 0x0006
    SWITCH_ON_CMD       = QUICK_STOP | ENABLE_VOLTAGE | SWITCH_ON # 0x0007
    ENABLE_OP           = SWITCH_ON_CMD | ENABLE_OPERATION        # 0x000F
    DISABLE_VOLTAGE     = 0x0000


class SW:
    """Statusword (0x6041) bit masks."""
    READY_TO_SWITCH_ON  = 0x0001
    SWITCHED_ON         = 0x0002
    OPERATION_ENABLED   = 0x0004
    FAULT               = 0x0008
    VOLTAGE_ENABLED     = 0x0010
    QUICK_STOP_ACTIVE   = 0x0020
    SWITCH_ON_DISABLED  = 0x0040
    WARNING             = 0x0080
    TARGET_REACHED      = 0x0400
    HOMING_ATTAINED     = 0x1000


# ---------------------------------------------------------------------------
# Command Enum — equivalent to DevDriveCMNDS.ctl
# ---------------------------------------------------------------------------
class DevDriveCMNDS(enum.Enum):
    """
    Command enum mirroring the LabVIEW DevDriveCMNDS.ctl type definition.
    Each value maps to a handler in the QMH dispatch loop.
    """
    INITIALIZE          = "Initialize"
    READ_STATUS         = "ReadStatus"
    READ_ENCODER        = "ReadEncoder"
    CALC_DEGS_FROM_CAL  = "CalcDegs_fromCal"
    UPDATE_MOTION       = "UpdateMotion"
    PERFORM_CALIBRATION = "PerformCalibration"
    PERFORM_MOVE        = "PerformMove"
    STOP                = "Stop"
    SHUTDOWN            = "Shutdown"


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class AxisConfig:
    """Configuration for a single servo axis."""
    ip_address: str = "192.168.1.1"
    port: int = 502
    unit_id: int = 1
    encoder_counts_per_rev: int = 1_280_000  # Delta 17-bit: 131072, or with 4x = 1280000
    gear_ratio: float = 1.0                   # mechanical gear ratio
    cal_offset_deg: float = 0.0               # calibration offset in degrees
    max_velocity_rpm: float = 3000.0
    accel_ms: float = 500.0                   # acceleration time in ms
    decel_ms: float = 500.0                   # deceleration time in ms
    home_method: int = 35                      # homing method per CIA 402


@dataclass
class AxisState:
    """Runtime state for a single servo axis."""
    connected: bool = False
    status_word: int = 0
    error_code: int = 0
    raw_encoder: int = 0
    position_deg: float = 0.0
    velocity_rpm: float = 0.0
    is_enabled: bool = False
    is_fault: bool = False
    is_homed: bool = False
    target_reached: bool = False
    cal_offset_counts: int = 0


@dataclass
class MotionCommand:
    """Payload for a motion command."""
    axis: int = 0               # 0 = axis A, 1 = axis B, -1 = both
    target_deg: float = 0.0
    velocity_rpm: float = 0.0   # 0 = use profile default
    relative: bool = False
    command: DevDriveCMNDS = DevDriveCMNDS.READ_STATUS


@dataclass
class DualDriverConfig:
    """Top-level configuration for the dual-axis driver."""
    axis_a: AxisConfig = field(default_factory=lambda: AxisConfig(
        ip_address="192.168.1.1", unit_id=1
    ))
    axis_b: AxisConfig = field(default_factory=lambda: AxisConfig(
        ip_address="192.168.1.2", unit_id=1
    ))
    polling_interval_s: float = 0.05   # 50 ms status polling


# ---------------------------------------------------------------------------
# Single-Axis Modbus Interface (sub-VI equivalents)
# ---------------------------------------------------------------------------
class DeltaServoAxis:
    """
    Low-level Modbus interface to a single Delta ASDA servo drive.
    Implements the sub-VI functions: ReadStatus, ReadEncoder,
    CalcDegs_fromCal, UpdateMotion, PerformCalibration, PerformMove.
    """

    def __init__(self, config: AxisConfig, name: str = "Axis"):
        self.config = config
        self.name = name
        self.state = AxisState()
        self._client: Optional[ModbusTcpClient] = None
        self._lock = threading.Lock()

    # -- Connection management (Create Master Instance / Close) --

    def connect(self) -> bool:
        """Create Modbus TCP master instance — mirrors Create Master Instance (TCP).vi."""
        with self._lock:
            try:
                self._client = ModbusTcpClient(
                    host=self.config.ip_address,
                    port=self.config.port,
                    timeout=3.0,
                )
                connected = self._client.connect()
                self.state.connected = connected
                if connected:
                    logger.info(f"{self.name}: Connected to {self.config.ip_address}:{self.config.port}")
                else:
                    logger.error(f"{self.name}: Failed to connect to {self.config.ip_address}")
                return connected
            except Exception as e:
                logger.error(f"{self.name}: Connection error: {e}")
                self.state.connected = False
                return False

    def disconnect(self):
        """Close Modbus master — mirrors Modbus Master Close.vi."""
        with self._lock:
            if self._client:
                try:
                    # Disable drive before disconnecting
                    self._write_register(DeltaReg.CONTROL_WORD, CW.DISABLE_VOLTAGE)
                except Exception:
                    pass
                self._client.close()
                self._client = None
            self.state.connected = False
            logger.info(f"{self.name}: Disconnected")

    # -- Low-level Modbus helpers --

    def _read_register(self, address: int, count: int = 1) -> list[int]:
        """Read holding register(s). Raises on failure."""
        if not self._client or not self.state.connected:
            raise ConnectionError(f"{self.name}: Not connected")
        result = self._client.read_holding_registers(
            address, count, slave=self.config.unit_id
        )
        if result.isError():
            raise ModbusException(f"{self.name}: Read error at 0x{address:04X}: {result}")
        return list(result.registers)

    def _write_register(self, address: int, value: int):
        """Write a single holding register."""
        if not self._client or not self.state.connected:
            raise ConnectionError(f"{self.name}: Not connected")
        result = self._client.write_register(
            address, value, slave=self.config.unit_id
        )
        if result.isError():
            raise ModbusException(f"{self.name}: Write error at 0x{address:04X}: {result}")

    def _write_registers(self, address: int, values: list[int]):
        """Write multiple holding registers."""
        if not self._client or not self.state.connected:
            raise ConnectionError(f"{self.name}: Not connected")
        result = self._client.write_registers(
            address, values, slave=self.config.unit_id
        )
        if result.isError():
            raise ModbusException(f"{self.name}: Write error at 0x{address:04X}: {result}")

    @staticmethod
    def _to_signed32(lo: int, hi: int) -> int:
        """Combine two 16-bit registers into a signed 32-bit integer."""
        val = (hi << 16) | lo
        if val >= 0x80000000:
            val -= 0x100000000
        return val

    @staticmethod
    def _from_signed32(val: int) -> tuple[int, int]:
        """Split a signed 32-bit integer into (lo, hi) 16-bit register values."""
        if val < 0:
            val += 0x100000000
        lo = val & 0xFFFF
        hi = (val >> 16) & 0xFFFF
        return lo, hi

    # -- Sub-VI Equivalents --

    def read_status(self) -> AxisState:
        """
        ReadStatus.vi equivalent.
        Reads the CIA 402 statusword and error code, updates axis state.
        """
        with self._lock:
            try:
                regs = self._read_register(DeltaReg.STATUS_WORD, 1)
                sw = regs[0]
                self.state.status_word = sw
                self.state.is_enabled = bool(sw & SW.OPERATION_ENABLED)
                self.state.is_fault = bool(sw & SW.FAULT)
                self.state.target_reached = bool(sw & SW.TARGET_REACHED)
                self.state.is_homed = bool(sw & SW.HOMING_ATTAINED)

                # Read error code if in fault
                if self.state.is_fault:
                    err_regs = self._read_register(DeltaReg.ERROR_CODE, 1)
                    self.state.error_code = err_regs[0]
                else:
                    self.state.error_code = 0

            except Exception as e:
                logger.warning(f"{self.name}: ReadStatus error: {e}")
                self.state.connected = False

        return self.state

    def read_encoder(self) -> int:
        """
        ReadEncoder.vi equivalent.
        Reads the actual position from the encoder (32-bit value in counts).
        """
        with self._lock:
            try:
                regs = self._read_register(DeltaReg.ACTUAL_POSITION_LO, 2)
                raw = self._to_signed32(regs[0], regs[1])
                self.state.raw_encoder = raw

                # Also read velocity
                vel_regs = self._read_register(DeltaReg.ACTUAL_VELOCITY_LO, 2)
                self.state.velocity_rpm = self._to_signed32(vel_regs[0], vel_regs[1]) / 1000.0

            except Exception as e:
                logger.warning(f"{self.name}: ReadEncoder error: {e}")

        return self.state.raw_encoder

    def calc_degs_from_cal(self) -> float:
        """
        CalcDegs_fromCal.vi equivalent.
        Converts raw encoder counts to degrees using the calibration offset
        and encoder resolution.

        degrees = (raw_counts - cal_offset) / counts_per_deg
        where counts_per_deg = encoder_counts_per_rev * gear_ratio / 360.0
        """
        counts_per_deg = (
            self.config.encoder_counts_per_rev
            * self.config.gear_ratio
            / 360.0
        )
        if counts_per_deg == 0:
            return 0.0

        position_deg = (
            (self.state.raw_encoder - self.state.cal_offset_counts)
            / counts_per_deg
        )
        self.state.position_deg = position_deg
        return position_deg

    def update_motion(self, velocity_rpm: float = 0, accel_ms: float = 0, decel_ms: float = 0):
        """
        UpdateMotion.vi equivalent.
        Updates the profile velocity, acceleration, and deceleration parameters.
        Uses config defaults if arguments are 0.
        """
        vel = velocity_rpm if velocity_rpm > 0 else self.config.max_velocity_rpm
        acc = accel_ms if accel_ms > 0 else self.config.accel_ms
        dec = decel_ms if decel_ms > 0 else self.config.decel_ms

        with self._lock:
            try:
                # Profile velocity (in 0.1 rpm units for Delta ASDA)
                vel_counts = int(vel * 10)
                lo, hi = self._from_signed32(vel_counts)
                self._write_registers(DeltaReg.PROFILE_VELOCITY_LO, [lo, hi])

                # Acceleration (in ms)
                acc_val = int(acc)
                lo, hi = self._from_signed32(acc_val)
                self._write_registers(DeltaReg.PROFILE_ACCEL_LO, [lo, hi])

                # Deceleration (in ms)
                dec_val = int(dec)
                lo, hi = self._from_signed32(dec_val)
                self._write_registers(DeltaReg.PROFILE_DECEL_LO, [lo, hi])

                logger.debug(
                    f"{self.name}: UpdateMotion vel={vel} rpm, "
                    f"accel={acc} ms, decel={dec} ms"
                )
            except Exception as e:
                logger.error(f"{self.name}: UpdateMotion error: {e}")

    def perform_calibration(self):
        """
        PerformCalibration.vi equivalent.
        Records the current encoder position as the calibration zero offset.
        This sets cal_offset_counts so that CalcDegs_fromCal returns 0.0 at
        the current position.
        """
        self.read_encoder()
        self.state.cal_offset_counts = self.state.raw_encoder
        self.config.cal_offset_deg = 0.0
        logger.info(
            f"{self.name}: Calibration set — zero at encoder count "
            f"{self.state.cal_offset_counts}"
        )

    def perform_move(self, target_deg: float, relative: bool = False):
        """
        PerformMove.vi equivalent.
        Commands a profile position move to the specified angle in degrees.

        1. Convert degrees to encoder counts (accounting for cal offset)
        2. Write target position registers
        3. Trigger the move via controlword
        """
        counts_per_deg = (
            self.config.encoder_counts_per_rev
            * self.config.gear_ratio
            / 360.0
        )

        if relative:
            target_counts = int(target_deg * counts_per_deg)
        else:
            target_counts = int(target_deg * counts_per_deg) + self.state.cal_offset_counts

        with self._lock:
            try:
                # Ensure Profile Position mode
                self._write_register(DeltaReg.MODES_OF_OPERATION, 1)

                # Write target position
                lo, hi = self._from_signed32(target_counts)
                self._write_registers(DeltaReg.TARGET_POSITION_LO, [lo, hi])

                # Build controlword: enable + new set point
                cw = CW.ENABLE_OP | CW.NEW_SET_POINT
                if relative:
                    cw |= CW.ABS_REL
                self._write_register(DeltaReg.CONTROL_WORD, cw)

                # Toggle new set point bit off (edge-triggered)
                time.sleep(0.01)
                self._write_register(DeltaReg.CONTROL_WORD, CW.ENABLE_OP)

                logger.info(
                    f"{self.name}: PerformMove → {target_deg:.3f}° "
                    f"({'relative' if relative else 'absolute'}), "
                    f"counts={target_counts}"
                )
            except Exception as e:
                logger.error(f"{self.name}: PerformMove error: {e}")

    def enable_drive(self) -> bool:
        """CIA 402 state machine: transition to Operation Enabled."""
        with self._lock:
            try:
                # Shutdown → SwitchOn → EnableOperation
                for cw_val, delay in [
                    (CW.SHUTDOWN, 0.05),
                    (CW.SWITCH_ON_CMD, 0.05),
                    (CW.ENABLE_OP, 0.05),
                ]:
                    self._write_register(DeltaReg.CONTROL_WORD, cw_val)
                    time.sleep(delay)

                # Verify
                regs = self._read_register(DeltaReg.STATUS_WORD, 1)
                enabled = bool(regs[0] & SW.OPERATION_ENABLED)
                self.state.is_enabled = enabled
                if enabled:
                    logger.info(f"{self.name}: Drive enabled")
                else:
                    logger.warning(f"{self.name}: Drive enable failed, SW=0x{regs[0]:04X}")
                return enabled
            except Exception as e:
                logger.error(f"{self.name}: Enable error: {e}")
                return False

    def disable_drive(self):
        """Disable the servo drive."""
        with self._lock:
            try:
                self._write_register(DeltaReg.CONTROL_WORD, CW.DISABLE_VOLTAGE)
                self.state.is_enabled = False
                logger.info(f"{self.name}: Drive disabled")
            except Exception as e:
                logger.error(f"{self.name}: Disable error: {e}")

    def clear_fault(self):
        """Reset a fault condition."""
        with self._lock:
            try:
                self._write_register(DeltaReg.CONTROL_WORD, CW.FAULT_RESET)
                time.sleep(0.1)
                self._write_register(DeltaReg.CONTROL_WORD, 0x0000)
                logger.info(f"{self.name}: Fault cleared")
            except Exception as e:
                logger.error(f"{self.name}: Fault clear error: {e}")

    def stop(self):
        """Immediate halt."""
        with self._lock:
            try:
                self._write_register(
                    DeltaReg.CONTROL_WORD,
                    CW.ENABLE_OP | CW.HALT
                )
                logger.info(f"{self.name}: Stop commanded")
            except Exception as e:
                logger.error(f"{self.name}: Stop error: {e}")


# ---------------------------------------------------------------------------
# Dual-Axis Driver — Queued Message Handler (QMH) Pattern
# ---------------------------------------------------------------------------
class ACDeltaDeviceDriverDual:
    """
    Top-level dual-axis Delta servo driver.

    Mirrors the LabVIEW QMH architecture:
      - A command queue accepts (DevDriveCMNDS, MotionCommand) tuples
      - A worker thread dequeues and dispatches to the appropriate handler
      - Boolean controls (switches) for enable/disable of each axis

    Usage:
        config = DualDriverConfig(
            axis_a=AxisConfig(ip_address="192.168.1.1"),
            axis_b=AxisConfig(ip_address="192.168.1.2"),
        )
        driver = ACDeltaDeviceDriverDual(config)
        driver.initialize()
        driver.enqueue(DevDriveCMNDS.PERFORM_MOVE,
                       MotionCommand(axis=-1, target_deg=10.0))
        # ...
        driver.shutdown()
    """

    def __init__(self, config: Optional[DualDriverConfig] = None):
        self.config = config or DualDriverConfig()
        self.axis_a = DeltaServoAxis(self.config.axis_a, name="AxisA")
        self.axis_b = DeltaServoAxis(self.config.axis_b, name="AxisB")

        # Command queue — equivalent to LabVIEW queue
        self._cmd_queue: queue.Queue[tuple[DevDriveCMNDS, MotionCommand]] = queue.Queue()
        self._worker_thread: Optional[threading.Thread] = None
        self._running = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

        # Boolean switches (front-panel controls in LabVIEW)
        self.axis_a_enabled = False
        self.axis_b_enabled = False

    # -- Queue interface (Insert Queue Element / Remove Queue Element) --

    def enqueue(self, cmd: DevDriveCMNDS, payload: Optional[MotionCommand] = None):
        """Insert Queue Element — add a command to the queue."""
        if payload is None:
            payload = MotionCommand(command=cmd)
        else:
            payload.command = cmd
        self._cmd_queue.put((cmd, payload))
        logger.debug(f"Enqueued: {cmd.value}")

    # -- QMH Worker Loop --

    def _worker_loop(self):
        """
        Main QMH while-loop — mirrors the LabVIEW block diagram.
        Dequeues commands and dispatches to the case structure.
        """
        logger.info("QMH worker loop started")
        while self._running.is_set():
            try:
                cmd, payload = self._cmd_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            try:
                self._dispatch(cmd, payload)
            except Exception as e:
                logger.error(f"Command {cmd.value} failed: {e}")
            finally:
                self._cmd_queue.task_done()

        logger.info("QMH worker loop exited")

    def _dispatch(self, cmd: DevDriveCMNDS, payload: MotionCommand):
        """Case structure — dispatch command to the appropriate handler."""
        axes = self._select_axes(payload.axis)

        if cmd == DevDriveCMNDS.INITIALIZE:
            self._handle_initialize()

        elif cmd == DevDriveCMNDS.READ_STATUS:
            for ax in axes:
                ax.read_status()

        elif cmd == DevDriveCMNDS.READ_ENCODER:
            for ax in axes:
                ax.read_encoder()

        elif cmd == DevDriveCMNDS.CALC_DEGS_FROM_CAL:
            for ax in axes:
                ax.read_encoder()
                ax.calc_degs_from_cal()

        elif cmd == DevDriveCMNDS.UPDATE_MOTION:
            for ax in axes:
                ax.update_motion(
                    velocity_rpm=payload.velocity_rpm,
                )

        elif cmd == DevDriveCMNDS.PERFORM_CALIBRATION:
            for ax in axes:
                ax.perform_calibration()

        elif cmd == DevDriveCMNDS.PERFORM_MOVE:
            for ax in axes:
                ax.perform_move(
                    target_deg=payload.target_deg,
                    relative=payload.relative,
                )

        elif cmd == DevDriveCMNDS.STOP:
            for ax in axes:
                ax.stop()

        elif cmd == DevDriveCMNDS.SHUTDOWN:
            self._handle_shutdown()

        else:
            logger.warning(f"Unknown command: {cmd}")

    def _select_axes(self, axis_idx: int) -> list[DeltaServoAxis]:
        """Select which axis/axes to operate on."""
        if axis_idx == 0:
            return [self.axis_a]
        elif axis_idx == 1:
            return [self.axis_b]
        else:  # -1 or any other → both
            return [self.axis_a, self.axis_b]

    # -- High-level operations --

    def _handle_initialize(self):
        """
        Initialize command — Create Master Instance (TCP).vi for both axes,
        enable drives, set default motion profile.
        """
        logger.info("Initializing dual-axis driver...")

        # Connect both axes
        self.axis_a.connect()
        self.axis_b.connect()

        # Enable drives
        if self.axis_a.state.connected:
            if self.axis_a.state.is_fault:
                self.axis_a.clear_fault()
                time.sleep(0.2)
            self.axis_a_enabled = self.axis_a.enable_drive()
            self.axis_a.update_motion()

        if self.axis_b.state.connected:
            if self.axis_b.state.is_fault:
                self.axis_b.clear_fault()
                time.sleep(0.2)
            self.axis_b_enabled = self.axis_b.enable_drive()
            self.axis_b.update_motion()

        logger.info(
            f"Init complete — A: {'OK' if self.axis_a_enabled else 'FAIL'}, "
            f"B: {'OK' if self.axis_b_enabled else 'FAIL'}"
        )

    def _handle_shutdown(self):
        """Shutdown command — disable drives and close connections."""
        logger.info("Shutting down dual-axis driver...")
        self.axis_a.disable_drive()
        self.axis_b.disable_drive()
        self.axis_a.disconnect()
        self.axis_b.disconnect()
        self.axis_a_enabled = False
        self.axis_b_enabled = False
        self._running.clear()

    def _poll_loop(self):
        """Background status polling loop — periodic ReadStatus + ReadEncoder."""
        while self._running.is_set():
            try:
                if self.axis_a.state.connected:
                    self.axis_a.read_status()
                    self.axis_a.read_encoder()
                    self.axis_a.calc_degs_from_cal()

                if self.axis_b.state.connected:
                    self.axis_b.read_status()
                    self.axis_b.read_encoder()
                    self.axis_b.calc_degs_from_cal()

            except Exception as e:
                logger.debug(f"Poll error: {e}")

            time.sleep(self.config.polling_interval_s)

    # -- Public API --

    def initialize(self):
        """Start the driver: connect, enable, and begin the QMH loop."""
        self._running.set()

        # Start worker thread (QMH loop)
        self._worker_thread = threading.Thread(
            target=self._worker_loop, daemon=True, name="QMH-Worker"
        )
        self._worker_thread.start()

        # Start polling thread
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="Status-Poll"
        )
        self._poll_thread.start()

        # Enqueue the initialize command
        self.enqueue(DevDriveCMNDS.INITIALIZE)

    def shutdown(self):
        """Stop the driver gracefully."""
        self.enqueue(DevDriveCMNDS.SHUTDOWN)
        # Wait for queue to drain
        self._cmd_queue.join()
        self._running.clear()
        if self._worker_thread:
            self._worker_thread.join(timeout=5.0)
        if self._poll_thread:
            self._poll_thread.join(timeout=5.0)
        logger.info("Driver shut down complete")

    def move_to(self, axis: int, degrees: float, velocity_rpm: float = 0):
        """Convenience: move an axis to an absolute position in degrees."""
        payload = MotionCommand(
            axis=axis,
            target_deg=degrees,
            velocity_rpm=velocity_rpm,
            relative=False,
        )
        self.enqueue(DevDriveCMNDS.PERFORM_MOVE, payload)

    def move_relative(self, axis: int, delta_deg: float, velocity_rpm: float = 0):
        """Convenience: move an axis by a relative amount in degrees."""
        payload = MotionCommand(
            axis=axis,
            target_deg=delta_deg,
            velocity_rpm=velocity_rpm,
            relative=True,
        )
        self.enqueue(DevDriveCMNDS.PERFORM_MOVE, payload)

    def calibrate(self, axis: int = -1):
        """Set the current position as zero for the specified axis/axes."""
        self.enqueue(DevDriveCMNDS.PERFORM_CALIBRATION, MotionCommand(axis=axis))

    def stop_all(self):
        """Emergency stop both axes."""
        self.enqueue(DevDriveCMNDS.STOP, MotionCommand(axis=-1))

    def get_positions(self) -> tuple[float, float]:
        """Return current (axis_a_deg, axis_b_deg)."""
        return (self.axis_a.state.position_deg, self.axis_b.state.position_deg)

    def get_status_summary(self) -> dict:
        """Return a summary dict for both axes."""
        def _ax_summary(ax: DeltaServoAxis, enabled: bool) -> dict:
            s = ax.state
            return {
                "connected": s.connected,
                "enabled": enabled and s.is_enabled,
                "fault": s.is_fault,
                "error_code": f"0x{s.error_code:04X}" if s.error_code else "None",
                "position_deg": round(s.position_deg, 4),
                "velocity_rpm": round(s.velocity_rpm, 2),
                "target_reached": s.target_reached,
                "homed": s.is_homed,
                "raw_encoder": s.raw_encoder,
            }
        return {
            "axis_a": _ax_summary(self.axis_a, self.axis_a_enabled),
            "axis_b": _ax_summary(self.axis_b, self.axis_b_enabled),
        }

    def wait_for_target(self, timeout_s: float = 30.0) -> bool:
        """Block until both axes report target_reached or timeout."""
        t0 = time.time()
        while (time.time() - t0) < timeout_s:
            a_done = (
                not self.axis_a.state.connected
                or self.axis_a.state.target_reached
            )
            b_done = (
                not self.axis_b.state.connected
                or self.axis_b.state.target_reached
            )
            if a_done and b_done:
                return True
            time.sleep(0.05)
        logger.warning("wait_for_target timed out")
        return False


# ---------------------------------------------------------------------------
# CLI / Demo
# ---------------------------------------------------------------------------
def main():
    """Example usage — configure and run the dual-axis driver."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Configure for your setup — adjust IPs, gear ratios, etc.
    config = DualDriverConfig(
        axis_a=AxisConfig(
            ip_address="192.168.1.1",
            port=502,
            unit_id=1,
            encoder_counts_per_rev=1_280_000,
            gear_ratio=1.0,
            max_velocity_rpm=1000.0,
            accel_ms=300.0,
            decel_ms=300.0,
        ),
        axis_b=AxisConfig(
            ip_address="192.168.1.2",
            port=502,
            unit_id=1,
            encoder_counts_per_rev=1_280_000,
            gear_ratio=1.0,
            max_velocity_rpm=1000.0,
            accel_ms=300.0,
            decel_ms=300.0,
        ),
        polling_interval_s=0.05,
    )

    driver = ACDeltaDeviceDriverDual(config)

    try:
        # Initialize (connect + enable + set motion profile)
        driver.initialize()
        time.sleep(2.0)  # Allow init to complete

        # Calibrate both axes at current position
        driver.calibrate(axis=-1)
        time.sleep(0.5)

        # Move axis A to +10 degrees
        print("Moving Axis A to +10°...")
        driver.move_to(axis=0, degrees=10.0)
        driver.wait_for_target(timeout_s=10.0)

        # Move axis B to -5 degrees
        print("Moving Axis B to -5°...")
        driver.move_to(axis=1, degrees=-5.0)
        driver.wait_for_target(timeout_s=10.0)

        # Print status
        status = driver.get_status_summary()
        print(f"\nAxis A: {status['axis_a']}")
        print(f"Axis B: {status['axis_b']}")

        pos_a, pos_b = driver.get_positions()
        print(f"\nPositions: A={pos_a:.3f}°, B={pos_b:.3f}°")

    except KeyboardInterrupt:
        print("\nInterrupted — stopping...")
        driver.stop_all()
    finally:
        driver.shutdown()


if __name__ == "__main__":
    main()
