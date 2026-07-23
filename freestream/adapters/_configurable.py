"""ConfigurableAdapter — expose each driver's own config + settings dialog.

Every device package ships a JSON-serialisable config dataclass
(``to_dict``/``from_dict``/``save``/``load``) and a Qt ``SettingsDialog(cfg,
parent)`` that mutates the config in place on ``accept()``. This mixin lets
the Freestream adapters surface those uniformly so that

* clicking a device card opens that device's OWN settings dialog
  (:meth:`open_settings`), and
* Save/Load Config round-trips EVERY device's driver config, not just the
  Freestream measurement config (:meth:`config_dict` / :meth:`apply_config_dict`).

Adapters set :attr:`settings_dialog_path` to the ``"pkg.app.settings_dialog:
SettingsDialog"`` dotted path and keep their driver config on ``self._cfg``
(the object the driver instance already holds a reference to — updates are
applied in place so the live device sees them).
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Dict, Optional

log = logging.getLogger(__name__)


class ConfigurableAdapter:
    """Mixin: uniform access to the wrapped driver's config + settings UI."""

    #: ``"package.app.settings_dialog:SettingsDialog"`` — the device's own
    #: dialog. Empty string = no native settings dialog available.
    settings_dialog_path: str = ""

    #: RS-232 devices: dotted path of the device package's comscan module
    #: (must expose ``search()`` returning ProbeResults) plus the result
    #: attribute that marks a positive hit (``is_sting``/``is_heise``).
    #: Empty string = not a scannable serial device. The standalone apps
    #: resolve the working COM port with the operator's Search button
    #: (comscan-then-connect); embedded in Freestream there is no such
    #: click, so :meth:`resolve_com_port`/:meth:`rescue_com_port` run the
    #: SAME scan at connect time.
    comscan_module: str = ""
    comscan_hit_attr: str = ""

    # ── config object (the live dataclass the driver holds) ──────────────
    @property
    def config(self) -> Any:
        return self._cfg

    @property
    def driver(self) -> Any:
        """The wrapped device-driver instance (CrescentDrive, Strainbook616,
        TunnelMonitor, …) — used by the embedded device panels that need a
        live device (axis calibration, encoder readouts)."""
        for attr in ("_drive", "_dev", "_monitor"):
            obj = getattr(self, attr, None)
            if obj is not None:
                return obj
        return None

    def config_dict(self) -> Dict[str, Any]:
        """JSON-ready snapshot of this device's driver config."""
        return self._cfg.to_dict()

    def apply_config_dict(self, data: Dict[str, Any]) -> None:
        """Load a saved driver config INTO the existing config object.

        Rebuilds a fresh dataclass via ``from_dict`` (so nested axis/channel
        objects reconstruct correctly) then copies its fields onto the live
        ``self._cfg`` — preserving the object identity the driver captured —
        and REBINDS the running driver (:meth:`rebind_driver_config`).

        The rebind is load-bearing for the positioners: ``from_dict`` builds
        NEW nested AxisConfig objects, so without ``driver.set_config`` the
        drive keeps reading the ORIGINAL axis objects and the loaded
        calibration/limits are silently ignored.

        ``force_sim`` is deliberately EXCLUDED from the restore: the
        manager's SIM/LIVE selection owns it (set at adapter build).
        Saved bundles capture whatever mode the snapshot happened to be
        taken in — restoring it silently flipped a LIVE session's driver
        back into the emulator while every badge still said LIVE (the
        adapter's own ``sim`` flag is separate). Rig-found 2026-07-22:
        the StrainBook "streamed wrong results / excitation frozen at
        10 V" in Freestream because a defaults bundle saved during a SIM
        session kept re-arming ``force_sim`` on the live driver.
        """
        if not data:
            return
        fresh = type(self._cfg).from_dict(data)
        session_sim = getattr(self._cfg, "force_sim", None)
        self._cfg.__dict__.update(fresh.__dict__)
        if session_sim is not None:
            self._cfg.force_sim = session_sim      # manager owns SIM/LIVE
        self.rebind_driver_config()

    def rebind_driver_config(self) -> None:
        """Point the live driver at ``self._cfg`` so edits apply NOW.

        Drivers with per-axis state (CrescentDrive, TraverseDrive) expose
        ``set_config`` exactly because a loaded/edited config must be
        rebound to the running drive; angles recompute immediately under a
        new calibration. Drivers without ``set_config`` read ``self._cfg``
        directly and need nothing here.
        """
        drv = self.driver
        if drv is not None and hasattr(drv, "set_config"):
            drv.set_config(self._cfg)

    # ── serial COM-port resolution (RS-232 adapters) ─────────────────────
    def resolve_com_port(self) -> None:
        """Ensure ``cfg.com_port`` names a port before a LIVE connect.

        No-op in sim (the emulator needs no port) and for adapters whose
        config has no ``com_port``. A BLANK port runs the device's own
        comscan ONCE and adopts the hit — the exact probe the standalone
        app's Search button uses; if nothing answers, fail with a clear,
        actionable message instead of pyserial's cryptic open error.
        """
        cfg = self._cfg
        if getattr(cfg, "force_sim", False) or not hasattr(cfg,
                                                           "com_port"):
            return
        if (cfg.com_port or "").strip():
            return
        if not self.comscan_module:
            raise RuntimeError(
                f"{self.id}: COM port not configured — set it in the "
                f"device settings dialog")
        log.info("%s: COM port not configured — running the device's "
                 "COM scan", self.id)
        found = self._comscan_once()
        if not found:
            raise RuntimeError(
                f"{self.id}: COM port not configured and the scan found "
                f"no {self.label} answering on any serial port — check "
                f"cabling/power, or set the port in the device settings "
                f"dialog, then reconnect")
        cfg.com_port = found

    def rescue_com_port(self) -> bool:
        """After a failed LIVE connect on a CONFIGURED port: scan once.

        Returns True (caller should retry connect) only when the scan
        finds the device answering on a DIFFERENT port — the embedded
        equivalent of the operator hitting Search after a failed
        Connect. Never touches the config otherwise, so the original
        (meaningful) connect error propagates.
        """
        cfg = self._cfg
        if getattr(cfg, "force_sim", False) or not self.comscan_module:
            return False
        current = (getattr(cfg, "com_port", "") or "").strip()
        log.info("%s: connect on %s failed — running the device's COM "
                 "scan", self.id, current or "(no port)")
        found = self._comscan_once()
        if not found or found == current:
            return False
        log.warning("%s: configured port %s did not answer but the scan "
                    "found the device on %s — retrying there",
                    self.id, current or "(none)", found)
        cfg.com_port = found
        return True

    def _comscan_once(self) -> Optional[str]:
        """One pass of the device package's ``comscan.search()``.

        Logs every probe summary (console visibility) and returns the
        first positive hit's port name, or None.
        """
        if not self.comscan_module:
            return None
        try:
            mod = importlib.import_module(self.comscan_module)
            results = mod.search()
        except Exception as exc:                       # noqa: BLE001
            log.warning("%s: COM scan failed: %s", self.id, exc)
            return None
        for r in results:
            log.info("%s comscan: %s", self.id, r.summary)
        hit = next((r for r in results
                    if getattr(r, self.comscan_hit_attr, False)), None)
        if hit is None:
            log.info("%s: COM scan found no answering device "
                     "(%d port(s) probed)", self.id, len(results))
            return None
        log.info("%s: COM scan found the device on %s", self.id,
                 hit.port.device)
        return hit.port.device

    # ── native settings dialog ───────────────────────────────────────────
    def has_settings(self) -> bool:
        return bool(self.settings_dialog_path)

    def open_settings(self, parent=None) -> bool:
        """Open the device's own settings dialog editing ``self._cfg``.

        Returns True if the user accepted (config mutated in place). The
        dialogs note that acquisition/protocol changes apply on the next
        connect; display/limit changes take effect immediately.
        """
        if not self.settings_dialog_path:
            return False
        module_name, cls_name = self.settings_dialog_path.split(":")
        dialog_cls = getattr(importlib.import_module(module_name), cls_name)
        dialog = dialog_cls(self._cfg, parent)
        return bool(dialog.exec())
