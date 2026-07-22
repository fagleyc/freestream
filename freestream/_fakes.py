"""Minimal fake adapters for registry/engine tests (no hardware, no Qt)."""

from __future__ import annotations

import time
from typing import Dict, List

import numpy as np

from freestream.hal import (OK, AxisSpec, ChannelSpec, DeviceStatus,
                           MoveHandle)


class FakeStreamer:
    """Streaming + Zeroable stand-in with a deterministic signal."""

    GROUP = "StrainBook_0"
    CHANNELS = ("N1", "N2", "Y1", "Y2", "Axial", "Roll", "Excitation")
    #: the four bridge names per balance layout (mirrors the real driver)
    _BRIDGE = {"Force": ("N1", "N2", "Y1", "Y2"),
               "Moment": ("AftPitch", "AftYaw", "FwdPitch", "FwdYaw")}
    _BRIDGE_INDEX = {n: i for names in _BRIDGE.values()
                     for i, n in enumerate(names)}

    def __init__(self, sim: bool = True, rate: float = 1000.0,
                 group: str = None, channels=None):
        self.id = "fake_streamer"
        self.label = "Fake Streamer"
        self._sim = sim
        self._rate = rate
        self._group = group or self.GROUP
        self._channels = tuple(channels or self.CHANNELS)
        self._balance_config = "Force"
        self._connected = False
        self._running = False
        self._t_started = 0.0
        self._drained_s = 0.0
        self.zero_calls = 0

    def connect(self):
        self._connected = True

    def disconnect(self):
        self._connected = False

    @property
    def connected(self):
        return self._connected

    @property
    def sim(self):
        return self._sim

    def status(self):
        state = OK if self._connected else "OFFLINE"
        age = 0.0 if self._running else None
        return DeviceStatus(state=state, sim=self._sim,
                            last_sample_age_s=age)

    def start(self):
        self._running = True
        self._t_started = time.perf_counter()
        self._drained_s = 0.0

    def stop(self):
        self._running = False

    def channels(self) -> List[ChannelSpec]:
        return [ChannelSpec(name=c, unit="V", group=self._group,
                            kind="raw", device_id=self.id)
                for c in self._channels]

    def latest(self) -> Dict[str, float]:
        return {c: 0.1 * i for i, c in enumerate(self._channels)}

    def drain_block(self) -> Dict[str, np.ndarray]:
        now = time.perf_counter() - self._t_started
        n = max(int((now - self._drained_s) * self._rate), 0)
        self._drained_s = now
        return {c: np.full(n, 0.1 * i)
                for i, c in enumerate(self._channels)}

    def sample_rate(self) -> float:
        return self._rate

    def set_sample_rate(self, hz: float) -> None:
        """Honor the suite-wide sample rate (mirrors the real adapters)."""
        self._rate = float(hz)

    def zero(self, seconds: float = 0.5) -> Dict[str, float]:
        self.zero_calls += 1
        return {c: 0.0 for c in self._channels}

    # ── balance layout (mirrors StrainbookAdapter's single source) ────────
    @property
    def balance_config(self) -> str:
        return self._balance_config

    @balance_config.setter
    def balance_config(self, value: str) -> None:
        target = self._BRIDGE.get(value)
        if target is None:
            raise ValueError(f"unknown balance_config {value!r}")
        self._channels = tuple(
            target[self._BRIDGE_INDEX[c]]
            if c in self._BRIDGE_INDEX else c
            for c in self._channels)
        self._balance_config = value


class FakeDaq(FakeStreamer):
    GROUP = "DaqBook2005"
    CHANNELS = ("Pdiff", "Ptot", "Temp")

    def __init__(self, sim: bool = True):
        super().__init__(sim=sim, rate=200.0)
        self.id = "fake_daq"
        self.label = "Fake DaqBook"

    def latest(self):
        return {"Pdiff": 0.44, "Ptot": 11.38, "Temp": 21.0}

    def drain_block(self):
        block = super().drain_block()
        vals = self.latest()
        return {c: np.full(len(a), vals[c]) for c, a in block.items()}

    def zero(self, seconds: float = 0.5):
        raise AssertionError("daq is not Zeroable in real life")


# strip zero() so FakeDaq does NOT satisfy Zeroable? Protocols check
# presence — keep FakeDaq's zero raising instead of removing (documents
# that manager must use roles, not blind zero calls, for tunnel devices).


class FakePositioner:
    """Instant-ish positioner with settle delay."""

    def __init__(self, sim: bool = True, settle_s: float = 0.2):
        self.id = "fake_positioner"
        self.label = "Fake Positioner"
        self._sim = sim
        self._connected = False
        self._pos = {"alpha": 0.0, "beta": 0.0}
        self._target = dict(self._pos)
        self._settle_at = 0.0
        self._settle_s = settle_s
        self.stopped = False

    def connect(self):
        self._connected = True

    def disconnect(self):
        self._connected = False

    @property
    def connected(self):
        return self._connected

    @property
    def sim(self):
        return self._sim

    def status(self):
        return DeviceStatus(state=OK if self._connected else "OFFLINE",
                            sim=self._sim)

    def move_to(self, **axes: float) -> MoveHandle:
        self._target.update(axes)
        self._settle_at = time.perf_counter() + self._settle_s
        return MoveHandle(targets=dict(axes))

    def axes(self) -> List[AxisSpec]:
        return [AxisSpec("alpha", "deg", -20, 20, 0.05),
                AxisSpec("beta", "deg", -20, 20, 0.05)]

    def positions(self) -> Dict[str, float]:
        if self.settled():
            self._pos = dict(self._target)
        return dict(self._pos)

    def settled(self) -> bool:
        return time.perf_counter() >= self._settle_at

    def stop_all(self) -> None:
        self.stopped = True
        self._settle_at = 0.0


class FakeTunnel:
    """SetpointDevice stand-in: instant ramp with a tiny delay."""

    def __init__(self, sim: bool = True):
        self.id = "fake_tunnel"
        self.label = "Fake Tunnel"
        self._sim = sim
        self._connected = False
        self._rpm = 0.0
        self._target = 0.0
        self._at = 0.0

    def connect(self):
        self._connected = True

    def disconnect(self):
        self._connected = False

    @property
    def connected(self):
        return self._connected

    @property
    def sim(self):
        return self._sim

    def status(self):
        return DeviceStatus(state=OK if self._connected else "OFFLINE",
                            sim=self._sim)

    def set_target(self, **kw: float) -> None:
        self._target = kw.get("rpm", 0.0)
        self._at = time.perf_counter() + 0.2

    def at_target(self) -> bool:
        if time.perf_counter() >= self._at:
            self._rpm = self._target
            return True
        return False

    def readback(self) -> Dict[str, float]:
        return {"rpm": self._rpm, "rpm_set": self._target}
