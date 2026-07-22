"""Simulated Red Lion gateway for hardware-free development and tests.

Exposes the same transport surface as
:class:`~tunnel_plc.gateway.ModbusGateway` (``connect/close/
read_registers/read_elements/write_element``) and models the plant
behind it:

* tunnel fan start pulse → Fan_Running after a spin-up delay, RPM ramps
  toward RPM_Set; stop pulse → ramp to zero,
* cooling fan start/stop lights,
* an injectable inverter fault (``set_fault``) that drops the fan,
* injectable comm failure (``fail_comms``) for staleness/reconnect
  tests,
* every write recorded in ``write_history`` as (t, address, value) so
  tests can verify momentary pulse shape and timing.

Registers are served in the emulator's configured word order so the
decode path is exercised end-to-end.
"""

from __future__ import annotations

import math
import threading
import time
from typing import List

from .gateway import GatewayError
from .registers import (BEARING_CAL, BEARING_TAGS, BLOCK1_ADDR,
                        BLOCK1_REGISTERS_EXT, BLOCK1_TAGS, BLOCK2_ADDR,
                        decode_u32, encode_u32, unscale_bearing)

RPM_RAMP_PER_S = 200.0      # sim spool rate
FAN_START_DELAY_S = 0.2     # contactor delay before Fan_Running


class SimGateway:
    """Plant-sim stand-in for ModbusGateway."""

    def __init__(self, word_order: str = "low_first",
                 rpm_scale: float = 1.0):
        self.word_order = word_order
        self.rpm_scale = rpm_scale
        self._lock = threading.Lock()
        self._connected = False
        self.fail_comms = False

        # plant state
        self.rpm_set = 0.0
        self.actual_rpm = 0.0
        self.fan_running = False
        self.cooling_running = False
        self.fault = False
        self.console_control = True
        self.bearing_temp_low = False
        self.oil_level_low = False
        # bearing temperatures: plausible warm-bearing values with a slow
        # drift; the fan running nudges them upward a little. Served in
        # RAW counts (tunnel_tags.csv cal) so the full decode path runs.
        self.bearing_temps = {"bearing_b1": 84.0, "bearing_b2": 86.5,
                              "bearing_b3": 85.2}
        self._bearing_phase = 0.0
        self._fan_start_at = None
        self._t_last = time.perf_counter()

        self.write_history: List[tuple] = []   # (time.time(), addr, value)

    # ── test hooks ──
    def set_fault(self, fault: bool) -> None:
        with self._lock:
            self.fault = fault
            if fault:
                self.fan_running = False

    # ── connection surface ──
    def connect(self) -> None:
        if self.fail_comms:
            raise GatewayError("sim: comms failed")
        self._connected = True
        self._t_last = time.perf_counter()

    def close(self) -> None:
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ── plant physics ──
    def _advance(self) -> None:
        now = time.perf_counter()
        dt = min(now - self._t_last, 1.0)
        self._t_last = now
        if self._fan_start_at is not None and \
                now >= self._fan_start_at and not self.fault:
            self.fan_running = True
            self._fan_start_at = None
        target = self.rpm_set if (self.fan_running and not self.fault) \
            else 0.0
        step = RPM_RAMP_PER_S * dt
        if self.actual_rpm < target:
            self.actual_rpm = min(self.actual_rpm + step, target)
        else:
            self.actual_rpm = max(self.actual_rpm - step, target)
        self._bearing_phase += dt * 0.2      # slow bearing-temp drift

    def _block1_values(self) -> List[int]:
        v = {
            "rpm_set": int(round(self.rpm_set / self.rpm_scale)),
            "actual_rpm": int(round(self.actual_rpm / self.rpm_scale)),
            "tunnel_fan_stop_button": 0,
            "tunnel_fan_start_button": 0,
            "cooling_fan_start_button": 0,
            "cooling_fan_stop_button": 0,
            "bearing_heater_on": 0,
            "bearing_temp_low": int(self.bearing_temp_low),
            "fan_running": int(self.fan_running),
            "console_control": int(self.console_control),
            "oil_level_low": int(self.oil_level_low),
            "inverter_fault": int(self.fault),
            "tunnel_fan_light_start": int(self.fan_running),
            "tunnel_fan_light_stop": int(not self.fan_running),
            "cooling_fan_light_start": int(self.cooling_running),
            "cooling_fan_light_stop": int(not self.cooling_running),
        }
        values = [v[attr] for (_tag, attr, _b) in BLOCK1_TAGS]
        # extended elements 17–19: bearing temps as raw counts
        for i, (_tag, attr) in enumerate(BEARING_TAGS):
            temp = (self.bearing_temps[attr]
                    + 1.5 * math.sin(self._bearing_phase + 2.1 * i)
                    + 3.0 * self.actual_rpm / 1000.0)
            values.append(unscale_bearing(temp, BEARING_CAL[attr]))
        return values

    # ── protocol surface ──
    def read_registers(self, address: int, count: int) -> List[int]:
        with self._lock:
            if not self._connected or self.fail_comms:
                raise GatewayError("sim: comms failed")
            self._advance()
            if address != BLOCK1_ADDR or count > BLOCK1_REGISTERS_EXT:
                raise GatewayError(f"sim: unmapped read @{address}")
            regs: List[int] = []
            for value in self._block1_values():
                regs.extend(encode_u32(value, self.word_order))
            return regs[:count]

    def read_elements(self, address: int, n_elements: int) -> List[int]:
        regs = self.read_registers(address, n_elements * 2)
        return [decode_u32(regs[2 * i], regs[2 * i + 1], self.word_order)
                for i in range(n_elements)]

    def write_element(self, address: int, value: int) -> None:
        with self._lock:
            if not self._connected or self.fail_comms:
                raise GatewayError("sim: comms failed")
            self._advance()
            self.write_history.append((time.time(), address, int(value)))
            if address == BLOCK2_ADDR["RPM_Set"]:
                self.rpm_set = float(value) * self.rpm_scale
            elif address == BLOCK2_ADDR["Tunnel_Fan_Start_Button"]:
                if value and not self.fault:
                    self._fan_start_at = (time.perf_counter() +
                                          FAN_START_DELAY_S)
            elif address == BLOCK2_ADDR["Tunnel_Fan_Stop_Button"]:
                if value:
                    self.fan_running = False
                    self._fan_start_at = None
            elif address == BLOCK2_ADDR["Cooling_Fan_Start_Button"]:
                if value:
                    self.cooling_running = True
            elif address == BLOCK2_ADDR["Cooling_Fan_Stop_Button"]:
                if value:
                    self.cooling_running = False
            else:
                raise GatewayError(f"sim: unmapped write @{address}")
