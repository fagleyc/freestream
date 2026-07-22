"""DeviceManager — the registry that wires modes to adapters.

Devices are declared in ``devices_manifest.json`` (adapter dotted path +
role names per mode). Adding hardware is a new adapter module plus one
manifest entry — no orchestrator/GUI changes (cards, channels and
recording all derive from the registry).
"""

from __future__ import annotations

import importlib
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .hal import (DeviceBase, Positioner, SetpointDevice, Streaming,
                  Zeroable, capabilities)

log = logging.getLogger(__name__)

# make the existing driver packages importable (they live side by side
# under projects/devices/)
_DEVICES_DIR = Path(__file__).resolve().parents[1] / "devices"
if str(_DEVICES_DIR) not in sys.path:
    sys.path.insert(0, str(_DEVICES_DIR))

DEFAULT_MANIFEST = Path(__file__).resolve().parent / "devices_manifest.json"


class DeviceManager:
    """Instantiates the adapters for a mode and answers capability queries."""

    #: mode label used when the device set is picked device-by-device
    #: (Custom mode): roles are inferred from capabilities, not the manifest.
    CUSTOM = "custom"

    def __init__(self, mode: str = "mode1", sim: bool = True,
                 manifest_path: Optional[Path] = None,
                 custom_devices: Optional[List[str]] = None):
        self.mode = mode
        self.sim = sim
        self.manifest_path = Path(manifest_path or DEFAULT_MANIFEST)
        self.manifest = json.loads(self.manifest_path.read_text(
            encoding="utf-8"))
        #: explicit id list when the operator picked devices one-by-one;
        #: None for a normal manifest mode (mode1/mode2/mode3).
        self.custom_devices: Optional[List[str]] = (
            list(custom_devices) if custom_devices is not None else None)
        self.devices: Dict[str, DeviceBase] = {}
        # extra record interlocks contributed by the GUI (e.g. balance
        # overstress from the live Forces monitor). Each is a zero-arg
        # callable returning a blocker string, or None when clear.
        self.extra_blockers: List = []
        if self.custom_devices is not None:
            self.mode = self.CUSTOM
            self._build_custom()
            # roles are DERIVED from what the chosen adapters can do,
            # not read from manifest["modes"] (there is no custom mode).
            self.roles: Dict[str, str] = self._derive_roles()
        else:
            if mode not in self.manifest["modes"]:
                raise ValueError(f"unknown mode {mode!r}; manifest has "
                                 f"{list(self.manifest['modes'])}")
            self.roles = dict(self.manifest["modes"][mode])
            self._build()

    @classmethod
    def custom(cls, device_ids: List[str], sim: bool = True,
               manifest_path: Optional[Path] = None) -> "DeviceManager":
        """Build a manager from an EXPLICIT device-id subset (Custom mode).

        Roles (positioner / tunnel / balance) are inferred from the HAL
        capabilities of the chosen adapters rather than a manifest mode."""
        return cls(mode=cls.CUSTOM, sim=sim, manifest_path=manifest_path,
                   custom_devices=device_ids)

    # ── construction ─────────────────────────────────────────────────────
    def _instantiate(self, dev_id: str) -> DeviceBase:
        entry = self.manifest["devices"][dev_id]
        module_name, cls_name = entry["adapter"].rsplit(".", 1)
        cls = getattr(importlib.import_module(module_name), cls_name)
        adapter = cls(sim=self.sim, **entry.get("options", {}))
        adapter.id = dev_id
        log.info("registered %s (%s): %s", dev_id, adapter.label,
                 ", ".join(capabilities(adapter)) or "base")
        return adapter

    def _build(self) -> None:
        wanted = set(self.roles.values())
        for dev_id, entry in self.manifest["devices"].items():
            if dev_id not in wanted or not entry.get("enabled", True):
                continue
            self.devices[dev_id] = self._instantiate(dev_id)

    def _build_custom(self) -> None:
        """Build EXACTLY the chosen device ids (the operator's explicit
        pick — the manifest ``enabled`` flag is not consulted here)."""
        if not self.custom_devices:
            raise ValueError("custom device set is empty — pick at least "
                             "one device")
        unknown = [d for d in self.custom_devices
                   if d not in self.manifest["devices"]]
        if unknown:
            raise ValueError(f"unknown custom device(s) {unknown}; manifest "
                             f"has {list(self.manifest['devices'])}")
        for dev_id in self.custom_devices:
            self.devices[dev_id] = self._instantiate(dev_id)

    def _derive_roles(self) -> Dict[str, str]:
        """Infer role → device-id from capabilities (Custom mode).

        first Positioner → ``positioner``; first SetpointDevice →
        ``tunnel``; first Zeroable (else first Streaming) → ``balance``;
        the DaqBook-group streamer (else any other streamer) →
        ``tunnel_conditions``. Whatever isn't present is simply absent —
        the rail/monitors/dashboard all guard on ``None``."""
        roles: Dict[str, str] = {}
        for dev_id, dev in self.devices.items():
            if isinstance(dev, Positioner):
                roles.setdefault("positioner", dev_id)
            if isinstance(dev, SetpointDevice):
                roles.setdefault("tunnel", dev_id)
        for dev_id, dev in self.devices.items():
            if isinstance(dev, Zeroable):
                roles.setdefault("balance", dev_id)
        for dev_id, dev in self.devices.items():
            if not isinstance(dev, Streaming):
                continue
            roles.setdefault("balance", dev_id)     # fallback: no Zeroable
            try:
                if any(ch.group == "DaqBook2005" for ch in dev.channels()):
                    roles.setdefault("tunnel_conditions", dev_id)
            except Exception:                          # noqa: BLE001
                pass
        return roles

    # ── lifecycle ────────────────────────────────────────────────────────
    def connect_all(self) -> Dict[str, Exception]:
        """Connect every registered device; returns {id: error} for fails."""
        errors: Dict[str, Exception] = {}
        for dev_id, dev in self.devices.items():
            try:
                dev.connect()
            except Exception as exc:                   # noqa: BLE001
                log.exception("connect %s failed", dev_id)
                errors[dev_id] = exc
        return errors

    def disconnect_all(self) -> None:
        for dev in self.devices.values():
            try:
                dev.disconnect()
            except Exception:                          # noqa: BLE001
                log.exception("disconnect failed")

    def stop_all_motion(self) -> None:
        """E-stop path: stop every Positioner immediately."""
        for dev in self.devices.values():
            if isinstance(dev, Positioner):
                try:
                    dev.stop_all()
                except Exception:                      # noqa: BLE001
                    log.exception("stop_all failed on %s", dev.id)

    # ── capability queries ───────────────────────────────────────────────
    def by_role(self, role: str) -> Optional[DeviceBase]:
        dev_id = self.roles.get(role)
        return self.devices.get(dev_id) if dev_id else None

    @property
    def positioner(self) -> Optional[Positioner]:
        dev = self.by_role("positioner")
        return dev if isinstance(dev, Positioner) else None

    @property
    def setpoint(self) -> Optional[SetpointDevice]:
        dev = self.by_role("tunnel")
        return dev if isinstance(dev, SetpointDevice) else None

    @property
    def streaming(self) -> List[Streaming]:
        # de-duplicate: in Mode 2 the same ate adapter is balance AND
        # positioner; dict keys are unique device ids already
        return [d for d in self.devices.values() if isinstance(d, Streaming)]

    @property
    def zeroables(self) -> List[Zeroable]:
        return [d for d in self.devices.values() if isinstance(d, Zeroable)]

    def all_status(self) -> Dict[str, "DeviceStatus"]:
        return {dev_id: dev.status() for dev_id, dev in
                self.devices.items()}

    def record_blockers(self) -> List[str]:
        """Reasons recording must refuse (empty = clear to record)."""
        blockers = []
        for dev_id, dev in self.devices.items():
            st = dev.status()
            if not st.ok:
                blockers.append(f"{dev_id}: {st.state} {st.message}".strip())
        for hook in self.extra_blockers:
            try:
                msg = hook()
            except Exception:                          # noqa: BLE001
                continue
            if msg:
                blockers.append(str(msg))
        return blockers
