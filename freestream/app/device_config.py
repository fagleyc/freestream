"""DeviceConfigDialog — the full device-driver configuration GUI.

Clicking a device in the rail opens this dialog: a tabbed editor that
mirrors the device's standalone ``run_<device>`` app, assembled from

* a **Settings** tab — a :class:`~freestream.app.config_form.ConfigForm`
  over the driver config (communication properties, protocol options,
  safety limits — every scalar field EXCEPT the acquisition rate, which
  Freestream owns suite-wide in File → Measurement Setup, and the balance
  .vol/fit/layout, which are edited in the embedded StrainBook panel's
  Forces tab), with known enumerations (word order, connect mode…) as
  combos;
* **Axis** tabs for multi-axis positioners (crescent Alpha/Beta, traverse
  X/Y/Z) — the nested per-axis dataclasses (IP/port, protocol options).
  Calibration values are NEVER duplicated here (the Calibration tab is
  their single editor — a second editor's stale widgets would clobber a
  fresh calibration on Apply), and the crescent's motion limits are
  driver-config defaults rather than per-session fields;
* for EVERY device, the ENTIRE standalone device GUI embedded as the
  first tab, wired to the adapter's OWN driver instance — one device,
  one connection, with the Connection row hidden because Freestream owns
  the lifecycle:

  - crescent → CrescentPanel (axis cards, hold-to-run jog, speed steps,
    synchronous Move Both + E-STOP, angle history, calibration tab);
  - tunnel → TunnelPanel (RPM gauge, VersaMax status lights, RPM
    history, and the ARM-gated control section — the arming safety is
    IDENTICAL to the standalone app: no write-capable TunnelControl
    object exists until the operator arms writes, and arming still
    demands rpm_max > 0);
  - traverse → TraversePanel (X/Y/Z axis cards, per-axis STOP, E-STOP,
    Diagnostics module status/event log, calibration tab);
  - strainbook → StrainbookPanel (bridge tiles, history, the Forces
    load-limit monitor, per-channel Channels table);
  - daqbook → DaqbookPanel (channel tiles, history, Channels table);
  - ate → AteBalancePanel (live loads, motion drives, run/dwell — the
    device's single-slot callbacks are CHAINED so the adapter's own
    hooks keep firing, and restored on dialog close via detach());
  - lswt → LswtPanel (Hz gauge + rotor, flow tiles, strip charts, and
    the ARM-gated fan control — the arming safety is IDENTICAL to the
    standalone app: Start/Stop/Apply stay disabled until the operator
    arms fan control, and the E-STOP is always live);
  - lswt_sting → StingPanel (Alpha/Beta axis boxes with absolute Go /
    step jog / zeroing, Go Both, STOP ALL, angle history, Limits tab);
  - ni_daq → NiDaqPanel (bridge tiles, history, the Forces load-limit
    monitor, Channels table, Output & Trigger tab);
  - heise → HeisePanel (Ptot/Temp big-number tiles, live pressure-unit
    selector, zero/damping controls, stacked history plots).

  Panel widgets that would fork suite-owned policy are disabled inside
  the embedded panel (the DAQ scan-rate spins sit in the hidden
  Connection rows; the ATE sample-duration spin is disabled with a
  "set in Measurement Setup" tooltip).

Apply/OK also REBINDS the running driver to the edited config
(``adapter.rebind_driver_config`` → ``drive.set_config``) so calibration
and axis edits take effect immediately on a live device.

Buttons: OK / Apply / Cancel plus per-device **Save…/Load…** of the driver
config JSON. Cancel is a true revert — the config snapshot taken on open
is restored (the channel tables edit the live config in place).

Acquisition/protocol changes apply at the device's next Connect, exactly
as in the standalone apps.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtWidgets import (QDialog, QDialogButtonBox, QFileDialog,
                             QHBoxLayout, QLabel, QMessageBox, QPushButton,
                             QTabWidget, QVBoxLayout, QWidget)

from .. import theme
from ..hal import FAULT, OK, Streaming, capabilities
from .config_form import ConfigForm

# status-lamp pill styles (mirrors the device rail's traffic light)
_PILL_CSS = ("border-radius: 8px; padding: 2px 10px; font-weight: bold; "
             "font-size: 9pt;")
_LAMP_STYLE = {
    OK: f"background: {theme.SUCCESS}; color: white; {_PILL_CSS}",
    FAULT: f"background: {theme.ERROR}; color: white; {_PILL_CSS}",
    "OFFLINE": (f"background: {theme.SURFACE}; color: {theme.TEXT_DIM}; "
                f"{_PILL_CSS}"),
}


# ── per-device assembly specs ────────────────────────────────────────────
#: ``skip`` hides driver-config fields from the Settings FORM so each
#: setting has exactly ONE editor: the acquisition rate (``scan_hz``,
#: ``default_sample_seconds``) is the global sample rate in Measurement
#: Setup, and the balance ``.vol``/fit/layout pointers are edited in the
#: embedded StrainBook panel's Forces tab (this dialog's primary tab) —
#: Freestream's Forces page and Measurement Setup only display them.
def _spec(sections=(), choices=None, axes=(), channels_panel="",
          cal_panel="", skip=(), axis_skip=(),
          device_panel="", device_kwarg="drive",
          device_tab="Motion && Calibration") -> Dict[str, Any]:
    return {"sections": sections, "choices": choices or {}, "axes": axes,
            "channels_panel": channels_panel, "cal_panel": cal_panel,
            "skip": tuple(skip), "axis_skip": tuple(axis_skip),
            "device_panel": device_panel, "device_kwarg": device_kwarg,
            "device_tab": device_tab}


AXIS_SECTIONS = (
    ("Communication", ("ip", "port", "unit_id")),
    ("Protocol", ("stop_value", "invert_direction", "enabled",
                  "fwd_increases_counts", "wrap_modulus",
                  "limit_enabled")),
    ("Motion & limits", ("min_deg", "max_deg", "tolerance_deg", "min_in",
                         "max_in", "tolerance_in")),
    # traverse host-side homing: jog to the NEGATIVE limit switch
    # (StatusWord bit) and calibrate the offset at the datum — X ships
    # home_enabled=False (no homing on the axial axis)
    ("Homing (host)", ("home_enabled", "home_datum_in",
                       "home_jog_fwd")),
    ("PLC mapping", ("fwd_mask", "rev_mask", "position_addr")),
)

#: Calibration values have exactly ONE editor — the live Calibration tab
#: (two-point capture / offset re-zero / constants). Duplicating them in
#: the axis tabs let a stale widget snapshot clobber a fresh calibration
#: when Apply/OK replayed every form.
AXIS_CAL_FIELDS = ("calibrated", "angle_high", "encoder_high",
                   "clicks_per_degree", "inch_high", "counts_high",
                   "clicks_per_inch")

DEVICE_SPECS: Dict[str, Dict[str, Any]] = {
    "crescent": _spec(
        sections=(
            ("Control loop", ("loop_ms", "max_step")),
            ("Communication", ("modbus_timeout_s", "max_consecutive_errors")),
            ("Display", ("plot_window_s",)),
        ),
        axes=(("Alpha axis", "alpha"), ("Beta axis", "beta")),
        # rig travel limits/tolerance are driver-config defaults
        # (ac_delta.config._alpha/_beta), not per-session dialog fields
        axis_skip=("min_deg", "max_deg", "tolerance_deg"),
        # the standalone app's complete panel (motion cards + jog + sync
        # move + plot + calibration tab) embeds as the primary tab
        device_panel="ac_delta.app.main_window:CrescentPanel",
    ),
    "strainbook": _spec(
        sections=(
            ("Communication", ("device_name", "device_ip", "dll_path")),
            ("Buffering", ("buffer_seconds", "poll_ms")),
            ("Balance identity", ("balance_type", "balance_serial")),
            ("Display", ("plot_window_s", "tile_avg_ms")),
        ),
        # scan_hz follows the suite-wide sample rate (Measurement Setup);
        # the .vol/fit/layout pointers are edited in the embedded panel's
        # Forces tab — the single editor the Freestream Forces page
        # inherits from (the internal excitation banks are gone — the rig
        # uses an external supply that the driver never commands)
        skip=("scan_hz", "vol_path", "cal_type", "balance_config",
              "warn_utilization"),
        # the standalone app's complete panel (live tiles + bridge history
        # + Forces load-limit monitor + Channels table) as the primary tab
        device_panel="strainbook_616.app.main_window:StrainbookPanel",
        device_kwarg="device", device_tab="Live && Channels",
    ),
    "daqbook": _spec(
        sections=(
            ("Communication", ("device_name", "device_ip", "dll_path")),
            ("Buffering", ("buffer_seconds", "poll_ms")),
            ("Display", ("plot_window_s", "tile_avg_ms")),
        ),
        skip=("scan_hz",),            # suite-wide sample rate owns this
        # the standalone app's complete panel (live tiles + channel
        # history + Channels table) as the primary tab
        device_panel="daqbook_2000.app.main_window:DaqbookPanel",
        device_kwarg="device", device_tab="Live && Channels",
    ),
    "ate": _spec(
        sections=(
            ("Communication", ("ogi_ip", "bind_host", "tmsc_port",
                               "tmsd_port", "ogit_port", "connect_mode",
                               "auto_trigger")),
            ("Reduction reference", ("rho_kg_m3",)),
            ("Display", ("plot_window_s", "bar_avg_ms")),
        ),
        choices={"connect_mode": ("listen", "dial")},
        # acquisition timing is Freestream's samples/dwell, not the OGI's;
        # the model-span mapping (span_config) has exactly ONE editor —
        # the embedded panel's Motion tab combo (which relabels the drive
        # boxes live) — a second Settings-form editor would clobber it on
        # Apply, exactly like the strainbook excitation banks; the rated
        # max_loads dict is edited in the device's own settings dialog,
        # never as a raw dict in this generic form
        skip=("default_sample_seconds", "span_config", "max_loads"),
        # the standalone app's complete panel (live loads + motion drives
        # + run/dwell) as the primary tab
        device_panel="ate_balance.app.main_window:AteBalancePanel",
        device_kwarg="device", device_tab="Live && Motion && Run",
    ),
    "tunnel": _spec(
        sections=(
            ("Communication", ("ip", "port", "unit_id", "modbus_timeout_s")),
            ("Monitor", ("poll_s", "stale_after_s", "backoff_min_s",
                         "backoff_max_s")),
            ("Protocol", ("word_order", "word_order_verified", "rpm_scale")),
            ("Write safety", ("rpm_max", "button_hold_ms",
                              "momentary_verified")),
            ("Display", ("plot_window_s",)),
        ),
        choices={"word_order": ("low_first", "high_first")},
        # the standalone app's complete panel (gauge + status lights + RPM
        # history + the ARM-gated control section) as the primary tab; the
        # arming safety is untouched — no TunnelControl exists until the
        # operator arms writes in the panel
        device_panel="tunnel_plc.app.main_window:TunnelPanel",
        device_kwarg="monitor", device_tab="Monitor && Control",
    ),
    "traverse": _spec(
        sections=(
            ("Communication", ("ip", "port", "unit_id", "modbus_timeout_s",
                               "max_consecutive_errors")),
            ("Control loop", ("loop_ms", "direction_dwell_ms",
                              "max_reversals", "max_counts_per_tick")),
            ("Stall detection", ("wrongway_ticks", "stall_ticks",
                                 "stall_abort_ticks")),
            ("PLC options", ("read_module_status", "limit_active_low")),
            # host-side homing tuning (per-axis home_enabled/datum live
            # in the axis tabs' "Homing (host)" section)
            ("Homing (host)", ("home_backoff_margin_s",
                               "home_seek_timeout_s",
                               "home_backoff_timeout_s")),
            ("Display", ("plot_window_s",)),
        ),
        axes=(("X axis", "x"), ("Y axis", "y"), ("Z axis", "z")),
        # the standalone app's complete panel (axis cards + per-axis STOP
        # + E-STOP + Diagnostics + Calibration tabs) as the primary tab;
        # the separate cal_panel tab is gone — the embedded panel IS the
        # one calibration editor now (a second live editor risked
        # clobbering a fresh cal)
        device_panel="traverse_swt.app.main_window:TraversePanel",
    ),
    "lswt": _spec(
        sections=(
            ("Communication", ("ip", "port", "unit_id",
                               "modbus_timeout_s")),
            ("Control", ("max_hz", "ramp_hz_per_s", "reference_sign")),
            ("Monitor", ("poll_s", "stale_after_s")),
            ("Display", ("plot_window_s",)),
            ("Simulation", ("sim_tau_s",)),
        ),
        # tunnel/label are the adapter's IDENTITY, fixed when the mode
        # manifest builds it (North vs South) — never a per-session
        # dialog field (and "tunnel" is validated only in __post_init__,
        # which a raw form setattr would bypass)
        skip=("tunnel", "label"),
        # the standalone app's complete panel (Hz gauge + rotor + flow
        # tiles + strip charts + the ARM-gated fan control) as the
        # primary tab; the arming safety is untouched — Start/Stop/Apply
        # stay disabled until the operator arms fan control in the panel
        device_panel="lswt.app.main_window:LswtPanel",
        device_kwarg="device", device_tab="Monitor && Control",
    ),
    "lswt_sting": _spec(
        sections=(
            ("Communication", ("com_port", "baud", "serial_timeout_s")),
            ("Control loop", ("move_timeout_margin",
                              "max_consecutive_errors")),
            ("Position persistence", ("restore_position", "state_path")),
            ("Display", ("plot_window_s",)),
        ),
        # the embedded panel's Limits tab is the single live editor for
        # the soft travel limits, park behaviour, poll period and the
        # connect-time Z reset — a second Settings-form editor would
        # clobber it on Apply (and no Axis tabs: the per-axis zero
        # reference is the open-loop calibration, operator-set via the
        # panel's "Set Current Angle…" only)
        skip=("poll_ms", "park_on_disconnect", "park_alpha_deg",
              "init_reset"),
        # the standalone app's complete panel (Alpha/Beta axis boxes +
        # step jog + Go Both + STOP ALL + history + Limits) as the
        # primary tab
        device_panel="lswt_sting.app.main_window:StingPanel",
        device_kwarg="device", device_tab="Motion && Limits",
    ),
    "ni_daq": _spec(
        sections=(
            ("Communication", ("device_name",)),
            ("Buffering", ("buffer_seconds", "poll_ms")),
            ("Analog output", ("ao_update_hz",)),
            ("Balance identity", ("balance_type", "balance_serial")),
            ("Display", ("plot_window_s", "tile_avg_ms")),
        ),
        # scan_hz follows the suite-wide sample rate (Measurement Setup);
        # the .vol/fit/layout pointers are edited in the embedded panel's
        # Forces tab — the single editor, exactly like the strainbook
        # (trigger/AO channel setup lives in the embedded panel's
        # Output & Trigger tab — nested dataclasses never reach this form)
        skip=("scan_hz", "vol_path", "cal_type", "balance_config",
              "warn_utilization"),
        # the standalone app's complete panel (live tiles + bridge history
        # + Forces load-limit monitor + Channels table + Output & Trigger)
        # as the primary tab
        device_panel="ni_usb_6351.app.main_window:NiDaqPanel",
        device_kwarg="device", device_tab="Live && Channels",
    ),
    "heise": _spec(
        sections=(
            ("Communication", ("com_port", "baud", "timeout_s")),
            ("Polling", ("poll_s", "buffer_seconds",
                         "max_consecutive_errors")),
            ("Protocol", ("apply_units_on_connect",)),
        ),
        # poll_s stays editable: it IS the indicator's honest sample rate
        # (a slow serial instrument — the adapter deliberately has no
        # set_sample_rate, same honesty rule as the ATE); the pressure
        # unit is edited live in the embedded panel's unit combo (the
        # port objects are nested and never reach this form, and the
        # adapter re-asserts the canonical Ptot/Temp names)
        # the standalone app's complete panel (Ptot/Temp tiles + unit
        # selector + zero/damping + stacked history) as the primary tab
        device_panel="heise.app.main_window:HeisePanel",
        device_kwarg="device", device_tab="Live && History",
    ),
}


def _import_obj(dotted: str):
    module_name, cls_name = dotted.split(":")
    return getattr(importlib.import_module(module_name), cls_name)


class DeviceConfigDialog(QDialog):
    """Complete configuration GUI for one registered device adapter."""

    def __init__(self, adapter, parent=None, on_connect=None,
                 on_disconnect=None, on_save_defaults=None):
        super().__init__(parent)
        self.adapter = adapter
        self.spec = DEVICE_SPECS.get(getattr(adapter, "id", ""), _spec())
        self._snapshot = adapter.config_dict()        # Cancel restores this
        self.applied = False                          # any Apply/OK happened
        #: main-window hooks (console log + rail poll + _connected
        #: bookkeeping stay consistent when the dialog opens from
        #: Freestream); None → direct adapter calls (standalone/tests)
        self._on_connect = on_connect
        self._on_disconnect = on_disconnect
        self._on_save_defaults = on_save_defaults

        self.setWindowTitle(f"{getattr(adapter, 'label', adapter.id)} — "
                            "Device Configuration")
        self.setMinimumSize(860, 620)
        # settings-style dialog with LARGE content → real min/max buttons
        # so the operator can maximize it onto any monitor
        self.setWindowFlags(self.windowFlags()
                            | Qt.WindowType.WindowMinMaxButtonsHint)

        root = QVBoxLayout(self)
        root.setSpacing(8)
        root.addLayout(self._build_header())

        self.tabs = QTabWidget()
        root.addWidget(self.tabs, 1)
        self._forms: List[ConfigForm] = []
        self._cal_panel = None
        self._channels_panel = None
        self._device_panel = None
        self._build_tabs()

        root.addLayout(self._build_buttons())

        # live pump for the calibration panel's encoder/counts readouts
        self._pump = QTimer(self)
        self._pump.setInterval(400)
        self._pump.timeout.connect(self._pump_cal)
        self._pump.start()

    # ── header ───────────────────────────────────────────────────────────
    def _build_header(self) -> QHBoxLayout:
        head = QHBoxLayout()
        name = QLabel(getattr(self.adapter, "label", self.adapter.id))
        name.setStyleSheet("font-size: 13pt; font-weight: bold;")
        head.addWidget(name)
        caps = " · ".join(capabilities(self.adapter)) or "base"
        tags = QLabel(f"{self.adapter.id}  ·  {caps}")
        tags.setStyleSheet(f"color: {theme.TEXT_DIM};")
        head.addWidget(tags)
        head.addStretch(1)
        # status lamp (OFFLINE/OK/FAULT + SIM) polled by the 400 ms pump,
        # exactly like the device rail's traffic light
        self.lamp = QLabel("OFFLINE")
        head.addWidget(self.lamp)
        self.conn_btn = QPushButton("Connect")
        self.conn_btn.setToolTip(
            "Connect / disconnect THIS device without leaving the dialog "
            "(streams start on connect and stop before disconnect).")
        self.conn_btn.clicked.connect(self._toggle_connect)
        head.addWidget(self.conn_btn)
        self._refresh_state()
        return head

    def _refresh_state(self) -> None:
        connected = bool(getattr(self.adapter, "connected", False))
        try:
            st = self.adapter.status()
            state, sim = st.state, bool(st.sim)
        except Exception:                              # noqa: BLE001
            state = OK if connected else "OFFLINE"
            sim = bool(getattr(self.adapter, "sim", False))
        self.lamp.setText(state + (" · SIM" if sim else ""))
        self.lamp.setStyleSheet(_LAMP_STYLE.get(state,
                                                _LAMP_STYLE["OFFLINE"]))
        self.conn_btn.setText("Disconnect" if connected else "Connect")

    def _toggle_connect(self) -> None:
        """Connect/disconnect THIS device from inside the dialog.

        Routed through the main window's per-device connect/disconnect
        callbacks when opened from Freestream (console logging, rail
        poll and connect-state bookkeeping stay consistent); falls back
        to direct adapter calls (+ stream start/stop) standalone."""
        connected = bool(getattr(self.adapter, "connected", False))
        try:
            if connected:
                if callable(self._on_disconnect):
                    self._on_disconnect()
                else:
                    if isinstance(self.adapter, Streaming):
                        self.adapter.stop()
                    self.adapter.disconnect()
            else:
                if callable(self._on_connect):
                    self._on_connect()
                else:
                    self.adapter.connect()
                    if isinstance(self.adapter, Streaming):
                        self.adapter.start()
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.warning(self, "Device connect", str(exc))
        self._refresh_state()

    # ── tabs ─────────────────────────────────────────────────────────────
    def _build_tabs(self) -> None:
        self._stop_device_panel()
        self.tabs.clear()
        self._forms.clear()
        self._cal_panel = None
        self._channels_panel = None
        self._device_panel = None
        cfg = self.adapter.config

        page = QWidget()
        lay = QVBoxLayout(page)
        note = QLabel("Communication / sampling changes apply at the "
                      "device's next Connect.")
        note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        lay.addWidget(note)
        main_form = ConfigForm(cfg, sections=self.spec["sections"],
                               choices=self.spec["choices"],
                               skip=self.spec["skip"])
        self._forms.append(main_form)
        lay.addWidget(main_form, 1)
        self.tabs.addTab(page, "Settings")

        for title, attr in self.spec["axes"]:
            axis_cfg = getattr(cfg, attr, None)
            if axis_cfg is None:
                continue
            # "name" is the axis identity (state()/ring keys) — not a
            # user setting; cal fields belong to the Calibration tab only
            form = ConfigForm(axis_cfg, sections=AXIS_SECTIONS,
                              skip=("name",) + AXIS_CAL_FIELDS +
                                   self.spec["axis_skip"])
            self._forms.append(form)
            self.tabs.addTab(form, title)

        if self.spec["channels_panel"]:
            try:
                panel_cls = _import_obj(self.spec["channels_panel"])
                self._channels_panel = panel_cls(cfg)
                self.tabs.addTab(self._channels_panel, "Channels")
            except Exception as exc:                   # noqa: BLE001
                self.tabs.addTab(QLabel(f"channels panel unavailable: "
                                        f"{exc}"), "Channels")

        if self.spec["cal_panel"]:
            driver = getattr(self.adapter, "driver", None)
            if driver is not None:
                try:
                    panel_cls = _import_obj(self.spec["cal_panel"])
                    self._cal_panel = panel_cls(cfg, driver)
                    self.tabs.addTab(self._cal_panel, "Calibration")
                except Exception as exc:               # noqa: BLE001
                    self.tabs.addTab(QLabel(f"calibration panel "
                                            f"unavailable: {exc}"),
                                     "Calibration")

        if self.spec["device_panel"]:
            # the standalone device app's own panel, operating the SAME
            # driver instance the adapter owns (never a second connection);
            # its Connection row is hidden — Freestream owns the lifecycle.
            driver = getattr(self.adapter, "driver", None)
            if driver is not None:
                title = self.spec["device_tab"]
                try:
                    panel_cls = _import_obj(self.spec["device_panel"])
                    kwargs = {self.spec["device_kwarg"]: driver,
                              "embedded": True}
                    self._device_panel = panel_cls(cfg, **kwargs)
                    self.tabs.insertTab(0, self._device_panel, title)
                    self.tabs.setCurrentIndex(0)
                except Exception as exc:               # noqa: BLE001
                    self.tabs.addTab(QLabel(f"device panel unavailable: "
                                            f"{exc}"),
                                     title)

    def _pump_cal(self) -> None:
        """Feed live encoder/counts state into the calibration panel."""
        self._refresh_state()
        if self._cal_panel is None:
            return
        driver = getattr(self.adapter, "driver", None)
        if driver is None or not getattr(driver, "connected", False):
            return
        try:
            self._cal_panel.refresh(driver.state())
        except Exception:                              # noqa: BLE001
            pass

    # ── buttons ──────────────────────────────────────────────────────────
    def _build_buttons(self) -> QHBoxLayout:
        row = QHBoxLayout()
        save_btn = QPushButton("Save to file…")
        save_btn.clicked.connect(self._save_file)
        row.addWidget(save_btn)
        load_btn = QPushButton("Load from file…")
        load_btn.clicked.connect(self._load_file)
        row.addWidget(load_btn)
        self.defaults_btn = QPushButton("Set as Defaults")
        self.defaults_btn.setToolTip(
            "Apply, then store these settings in BOTH places:\n"
            "• the device's OWN startup-defaults file (when the device "
            "package has one — traverse, LSWT), used by its standalone "
            "app;\n"
            "• Freestream's startup-defaults bundle (this device's config "
            "is snapshotted into the suite defaults, auto-loaded on the "
            "next launch).")
        self.defaults_btn.clicked.connect(self._set_defaults)
        row.addWidget(self.defaults_btn)
        row.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok |
            QDialogButtonBox.StandardButton.Apply |
            QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        buttons.button(
            QDialogButtonBox.StandardButton.Apply).clicked.connect(
            self._apply)
        row.addWidget(buttons)
        return row

    # ── apply / revert semantics ─────────────────────────────────────────
    def _apply(self) -> None:
        for form in self._forms:
            form.apply()
        # rebind the RUNNING driver to the edited config so calibration /
        # axis changes apply immediately (drive.set_config re-derives the
        # displayed angles); without this the drive silently keeps reading
        # its original config objects.
        rebind = getattr(self.adapter, "rebind_driver_config", None)
        if callable(rebind):
            rebind()
        if self._device_panel is not None and \
                callable(getattr(self._device_panel, "apply_settings",
                                 None)):
            self._device_panel.apply_settings()       # target spin ranges
        self.applied = True
        self._snapshot = self.adapter.config_dict()   # new revert baseline

    def _stop_device_panel(self) -> None:
        """Quiesce the embedded device panel's own timers and detach any
        driver callbacks it claimed (the dialog can outlive its exec()
        through the Qt parent chain — nothing may keep firing into it)."""
        if self._device_panel is None:
            return
        for timer_name in ("_ui_timer", "_slow_timer"):
            timer = getattr(self._device_panel, timer_name, None)
            if timer is not None:
                try:
                    timer.stop()
                except Exception:                      # noqa: BLE001
                    pass
        detach = getattr(self._device_panel, "detach", None)
        if callable(detach):
            try:
                detach()
            except Exception:                          # noqa: BLE001
                pass

    def accept(self) -> None:                          # noqa: D102
        self._apply()
        self._pump.stop()
        self._stop_device_panel()
        super().accept()

    def reject(self) -> None:                          # noqa: D102
        # channel tables + the live cal/device panels edit the live config
        # → restore the open snapshot (apply_config_dict also rebinds the
        # running driver, so the revert reaches a connected drive too)
        self._pump.stop()
        self._stop_device_panel()
        try:
            self.adapter.apply_config_dict(self._snapshot)
        except Exception:                              # noqa: BLE001
            pass
        super().reject()

    # ── set as defaults (device-local file + freestream bundle) ─────────
    def _set_defaults(self) -> None:
        """Persist what's shown as BOTH the device package's own startup
        defaults (when it has a defaults_path()) and — via the main
        window's callback — Freestream's startup-defaults bundle."""
        self._apply()                                  # persist what's shown
        self._save_device_defaults()
        if callable(self._on_save_defaults):
            try:
                self._on_save_defaults()
            except Exception as exc:                   # noqa: BLE001
                QMessageBox.warning(self, "Set as Defaults", str(exc))

    def _save_device_defaults(self) -> Optional[Path]:
        """Save the device package's OWN startup-defaults file when its
        config module exposes a ``defaults_path()`` (traverse_swt, lswt
        — the lswt path is per-tunnel); silently skipped otherwise."""
        cfg = self.adapter.config
        try:
            mod = importlib.import_module(type(cfg).__module__)
            fn = getattr(mod, "defaults_path", None)
            if not callable(fn):
                return None
            tunnel = getattr(cfg, "tunnel", None)
            try:
                path = Path(fn(tunnel) if tunnel else fn())
            except TypeError:                          # fn takes no args
                path = Path(fn())
            path.parent.mkdir(parents=True, exist_ok=True)
            cfg.save(path)
            return path
        except Exception:                              # noqa: BLE001
            return None

    # ── config file I/O ──────────────────────────────────────────────────
    def _save_file(self) -> None:
        self._apply()                                  # persist what's shown
        path, _ = QFileDialog.getSaveFileName(
            self, "Save device config",
            f"{self.adapter.id}_config.json", "Config (*.json)")
        if not path:
            return
        try:
            self.adapter.config.save(path)
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.warning(self, "Save config", str(exc))

    def _load_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Load device config", "", "Config (*.json)")
        if not path:
            return
        try:
            data = json.loads(open(path, encoding="utf-8").read())
            self.adapter.apply_config_dict(data)
        except Exception as exc:                       # noqa: BLE001
            QMessageBox.warning(self, "Load config", str(exc))
            return
        self._build_tabs()                             # re-mirror new config
