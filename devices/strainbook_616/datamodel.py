"""Data structures for the StrainBook interface.

The channel set is user-configurable, so unlike the balance's fixed
:class:`MasterFrame` the ring buffer here is built from a dynamic field list:
``t`` plus, per channel, the engineering value under the channel *name* and
the raw voltage under ``<name>_V``.
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional, Sequence

import numpy as np


class ScanRingBuffer:
    """Pre-allocated numpy ring buffer with thread-safe block push / tail.

    Same access pattern as the ate_balance RingBuffer so AeroVIS can treat
    all device streams alike, but accepts whole scan *blocks* (2-D arrays)
    because the DaqBook delivers data in chunks, not single frames.
    """

    def __init__(self, fields: Sequence[str], capacity: int = 600_000):
        self._fields = tuple(fields)
        self._capacity = capacity
        self._data: Dict[str, np.ndarray] = {
            f: np.zeros(capacity, dtype=np.float64) for f in self._fields
        }
        self._head = 0
        self._count = 0
        self._lock = threading.Lock()

    @property
    def fields(self) -> tuple:
        return self._fields

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    def push_block(self, block: Dict[str, np.ndarray]) -> None:
        """Append ``n`` scans given as {field: 1-D array of length n}."""
        n = len(block[self._fields[0]])
        if n == 0:
            return
        if n > self._capacity:      # keep only the newest capacity scans
            block = {f: v[-self._capacity:] for f, v in block.items()}
            n = self._capacity
        with self._lock:
            start = self._head % self._capacity
            end = start + n
            for f in self._fields:
                v = np.asarray(block[f], dtype=np.float64)
                if end <= self._capacity:
                    self._data[f][start:end] = v
                else:
                    k = self._capacity - start
                    self._data[f][start:] = v[:k]
                    self._data[f][:end - self._capacity] = v[k:]
            self._head += n
            self._count = min(self._count + n, self._capacity)

    def tail(self, n: int,
             fields: Optional[Sequence[str]] = None) -> Dict[str, np.ndarray]:
        """Return the last ``n`` scans as a dict of numpy arrays (copies).

        ``fields`` limits the copy to those field names (unknown names are
        ignored) — at GUI refresh rates copying only what a consumer plots
        is a large win over cloning every channel twice per tick.
        """
        with self._lock:
            want = self._fields if fields is None else \
                tuple(f for f in fields if f in self._data)
            n = min(n, self._count)
            if n == 0:
                return {f: np.array([], dtype=np.float64) for f in want}
            head = self._head % self._capacity
            if head >= n:
                slc = slice(head - n, head)
                return {f: self._data[f][slc].copy() for f in want}
            out = {}
            for f in want:
                part1 = self._data[f][self._capacity - (n - head):]
                part2 = self._data[f][:head]
                out[f] = np.concatenate([part1, part2])
            return out

    def latest(self) -> Optional[Dict[str, float]]:
        t = self.tail(1)
        if t[self._fields[0]].size == 0:
            return None
        return {f: float(v[0]) for f, v in t.items()}

    def clear(self) -> None:
        with self._lock:
            self._head = 0
            self._count = 0

    def rename_fields(self, mapping: Dict[str, str]) -> None:
        """Rename ring fields in place (data preserved).

        Used when the balance layout switches on a LIVE device: the four
        bridge channels get new names (N1↔AftPitch …) but hold identical
        data, so only the field KEYS move — no reallocation, history kept.
        """
        if not mapping:
            return
        with self._lock:
            self._fields = tuple(mapping.get(f, f) for f in self._fields)
            for old, new in mapping.items():
                if old in self._data and old != new:
                    self._data[new] = self._data.pop(old)


def fields_for(channel_names: List[str]) -> List[str]:
    """Ring-buffer field list for a set of channel names."""
    fields = ["t"]
    for name in channel_names:
        fields.append(name)
        fields.append(f"{name}_V")
    return fields
