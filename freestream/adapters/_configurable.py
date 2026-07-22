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
from typing import Any, Dict, Optional


class ConfigurableAdapter:
    """Mixin: uniform access to the wrapped driver's config + settings UI."""

    #: ``"package.app.settings_dialog:SettingsDialog"`` — the device's own
    #: dialog. Empty string = no native settings dialog available.
    settings_dialog_path: str = ""

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
