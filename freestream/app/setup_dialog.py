"""Measurement Setup dialog — the CONFIG side (spec §7).

Deliberately distinct from the Sweep Planner: this edits the
:class:`freestream.config.FreestreamConfig` (operator, config name, data
root, acquisition defaults, output formats, tunnel waits, balance
reduction); the planner edits test points.

Grouped into clear sections — General / Acquisition / Output / Tunnel /
Balance — instead of one long form. Deliberately NOT here (config/JSON
only, by request): the per-device cal-file pointer table and the
"tare balances before every point" toggle (``zero_each_point`` — the
engine capability remains, default False).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QDoubleSpinBox, QFileDialog, QFormLayout,
                             QGridLayout, QGroupBox, QHBoxLayout, QLabel,
                             QLineEdit, QPushButton, QSpinBox, QVBoxLayout)

from .. import theme
from ..config import FreestreamConfig


class MeasurementSetupDialog(QDialog):
    """Sectioned measurement-config editor.

    ``device_ids`` is accepted (and ignored) for call-site compatibility —
    the per-device cal-pointer table was removed from the GUI; the
    ``cal_files`` config field remains editable via JSON.
    """

    def __init__(self, config: FreestreamConfig,
                 device_ids: Optional[Sequence[str]] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Measurement Setup")
        self.setMinimumWidth(660)

        lay = QVBoxLayout(self)
        lay.setSpacing(10)
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        # ── General ──────────────────────────────────────────────────────
        gen_box = QGroupBox("General")
        gen = QFormLayout(gen_box)
        self.operator_edit = QLineEdit(config.operator)
        gen.addRow("Operator", self.operator_edit)
        self.config_name_edit = QLineEdit(config.config_name)
        self.config_name_edit.setToolTip(
            "Folder-per-configuration name — files land under "
            "<data root>/<config name>/")
        gen.addRow("Config name", self.config_name_edit)
        root_row = QHBoxLayout()
        self.data_root_edit = QLineEdit(config.data_root)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self._browse_root)
        root_row.addWidget(self.data_root_edit)
        root_row.addWidget(browse)
        gen.addRow("Data root", root_row)
        grid.addWidget(gen_box, 0, 0)

        # ── Acquisition ──────────────────────────────────────────────────
        acq_box = QGroupBox("Acquisition")
        acq = QFormLayout(acq_box)
        self.rate_spin = QDoubleSpinBox()
        self.rate_spin.setRange(0.1, 1_000_000.0)
        self.rate_spin.setDecimals(1)
        self.rate_spin.setValue(config.sample_rate_hz)
        self.rate_spin.setSuffix(" Hz")
        self.rate_spin.setToolTip(
            "ONE suite-wide acquisition rate, pushed into every streaming "
            "device that supports it at connect. Devices with a fixed "
            "rate (e.g. the ATE frame stream) keep reporting their own.")
        acq.addRow("Sample rate", self.rate_spin)
        self.samples_spin = QSpinBox()
        self.samples_spin.setRange(1, 10_000_000)
        self.samples_spin.setValue(config.samples)
        acq.addRow("Samples / point", self.samples_spin)
        self.dwell_spin = QDoubleSpinBox()
        self.dwell_spin.setRange(0.0, 600.0)
        self.dwell_spin.setDecimals(2)
        self.dwell_spin.setValue(config.dwell_s)
        self.dwell_spin.setSuffix(" s")
        self.dwell_spin.setToolTip("Settle dwell before acquiring a point")
        acq.addRow("Dwell", self.dwell_spin)
        self.move_to_spin = QDoubleSpinBox()
        self.move_to_spin.setRange(1.0, 3600.0)
        self.move_to_spin.setValue(config.move_timeout_s)
        self.move_to_spin.setSuffix(" s")
        acq.addRow("Move timeout", self.move_to_spin)
        grid.addWidget(acq_box, 0, 1)

        # ── Output (formats + naming) ────────────────────────────────────
        out_box = QGroupBox("Output")
        out = QFormLayout(out_box)
        self.format_combo = QComboBox()
        self.format_combo.addItem("HDF5 (.h5)", "h5")
        self.format_combo.addItem("MATLAB (.mat)", "mat")
        self.format_combo.addItem("Excel (.xlsx)", "xlsx")
        current = str(getattr(config, "output_format", "h5")
                      or "h5").lower().lstrip(".")
        idx = self.format_combo.findData(current)
        self.format_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.format_combo.setToolTip(
            "Per-point file format — ONE primary file per test point, "
            "same run_NNNN_… basename, raw channel vectors plus the run "
            "metadata. Applies to the NEXT sweep; the run log shows each "
            "path as it is written.")
        out.addRow("File format", self.format_combo)
        fmt_note = QLabel("HDF5/.mat are readable by Streamlined for "
                          "reduction; .xlsx is for spreadsheet review.")
        fmt_note.setStyleSheet(f"color: {theme.TEXT_DIM};")
        fmt_note.setWordWrap(True)
        out.addRow(fmt_note)                           # spans both columns
        self.template_edit = QLineEdit(config.filename_template)
        self.template_edit.setPlaceholderText(
            "blank = run_NNNN_alpha_.._beta_.._mach_.._[up|dn]  "
            "(fields: {run} {alpha} {beta} {mach} {dir} {air_state} …)")
        out.addRow("Filename template", self.template_edit)
        grid.addWidget(out_box, 1, 0)

        # ── Tunnel (waits + control) ─────────────────────────────────────
        tun_box = QGroupBox("Tunnel")
        tun = QFormLayout(tun_box)
        self.tunnel_to_spin = QDoubleSpinBox()
        self.tunnel_to_spin.setRange(1.0, 3600.0)
        self.tunnel_to_spin.setValue(config.tunnel_timeout_s)
        self.tunnel_to_spin.setSuffix(" s")
        tun.addRow("Tunnel timeout", self.tunnel_to_spin)
        self.mach_settle_spin = QDoubleSpinBox()
        self.mach_settle_spin.setRange(0.0, 600.0)
        self.mach_settle_spin.setDecimals(1)
        self.mach_settle_spin.setValue(config.mach_settle_s)
        self.mach_settle_spin.setSuffix(" s")
        self.mach_settle_spin.setToolTip(
            "Measured Mach must hold within tolerance this long before "
            "the operator-wait dialog auto-proceeds")
        tun.addRow("Mach settle", self.mach_settle_spin)
        self.mach_tol_spin = QDoubleSpinBox()
        self.mach_tol_spin.setRange(0.001, 0.5)
        self.mach_tol_spin.setDecimals(3)
        self.mach_tol_spin.setSingleStep(0.005)
        self.mach_tol_spin.setValue(config.mach_tolerance)
        self.mach_tol_spin.setToolTip(
            "|measured − target| Mach band counted as 'at target'")
        tun.addRow("Mach tolerance", self.mach_tol_spin)
        self.tunnel_ctl_chk = QCheckBox(
            "Tunnel RPM control (needs writable Block2)")
        self.tunnel_ctl_chk.setChecked(config.tunnel_control_enabled)
        self.tunnel_ctl_chk.setToolTip(
            "Unchecked (default): MONITOR-ONLY — Freestream never writes "
            "fan RPM, because the Red Lion currently rejects all Block2 "
            "writes (Crimson fix pending). Mach/rpm points pause with an "
            "operator dialog until the measured Mach holds at the target, "
            "then record honestly.\n"
            "Checked: Freestream commands RPM through the Mach loop — "
            "only once the writable Block2 firmware is installed.")
        tun.addRow(self.tunnel_ctl_chk)                # spans both columns
        self.mach_check_chk = QCheckBox(
            "Verify Mach at each point (operator dialog / settle check)")
        self.mach_check_chk.setChecked(config.mach_check_enabled)
        self.mach_check_chk.setToolTip(
            "Checked (default): monitor-only mach/rpm points open the "
            "operator wait dialog and auto-proceed once the measured "
            "Mach holds in tolerance.\n"
            "Unchecked: the per-point Mach gate is skipped entirely — "
            "each point records immediately after positioning, without "
            "waiting for tunnel conditions (tunnel channels are still "
            "recorded honestly).")
        tun.addRow(self.mach_check_chk)                # spans both columns
        grid.addWidget(tun_box, 1, 1)

        # ── Balance (display-only reduction; .vol is DEVICE-OWNED) ──────
        bal_box = QGroupBox("Balance (display-only live forces)")
        bal = QFormLayout(bal_box)
        self.vol_note = QLabel(
            (Path(config.vol_path).name if config.vol_path else "none") +
            "   — set in the StrainBook device panel (Forces tab)")
        self.vol_note.setObjectName("dim")
        self.vol_note.setToolTip(
            "The balance .vol calibration, fit type and layout are edited "
            "only in the StrainBook device panel; the Forces page and the "
            "recorded metadata inherit them. NEVER applied to the raw "
            "data at capture time.")
        bal.addRow("Balance .vol file", self.vol_note)
        geom_row = QHBoxLayout()
        geom_row.setSpacing(8)
        self.area_spin = self._geom_spin(config.ref_area, " in²")
        self.chord_spin = self._geom_spin(config.ref_chord, " in")
        self.span_spin = self._geom_spin(config.ref_span, " in")
        for lbl, spin, tip in (
                ("S", self.area_spin, "Reference area"),
                ("c", self.chord_spin, "Reference chord"),
                ("b", self.span_spin, "Reference span")):
            tag = QLabel(lbl)
            tag.setToolTip(tip)
            spin.setToolTip(tip)
            geom_row.addWidget(tag)
            geom_row.addWidget(spin, 1)
        bal.addRow("Ref S / c / b", geom_row)
        grid.addWidget(bal_box, 2, 0, 1, 2)

        # ── Model / Test (inherited from an imported run sheet; §5) ──────
        model_box = QGroupBox("Model / Test (inherited from the run sheet)")
        mt = QGridLayout(model_box)
        mt.setHorizontalSpacing(10)
        mt.setVerticalSpacing(6)
        self.test_name_edit = QLineEdit(config.test_name)
        self.model_name_edit = QLineEdit(config.model_name)
        self.engineer_edit = QLineEdit(config.engineer)
        self.prefix_edit = QLineEdit(config.data_prefix)
        for col, (label, edit, tip) in enumerate((
                ("Test name", self.test_name_edit,
                 "Test / entry name (run-sheet Test Info)"),
                ("Model", self.model_name_edit,
                 "Model name / no. (run-sheet Test Info)"))):
            edit.setToolTip(tip)
            mt.addWidget(QLabel(label), 0, col * 2)
            mt.addWidget(edit, 0, col * 2 + 1)
        for col, (label, edit, tip) in enumerate((
                ("Engineer", self.engineer_edit,
                 "Test engineer (run-sheet Test Info)"),
                ("Data prefix", self.prefix_edit,
                 "Data file prefix (run-sheet Test Info)"))):
            edit.setToolTip(tip)
            mt.addWidget(QLabel(label), 1, col * 2)
            mt.addWidget(edit, 1, col * 2 + 1)
        self.ref_dims_lbl = QLabel(self._ref_dims_text(config))
        self.ref_dims_lbl.setStyleSheet(f"color: {theme.TEXT_DIM};")
        self.ref_dims_lbl.setToolTip(
            "Reference dimensions from the run sheet's Test Info tab — "
            "recorded in metadata for Streamlined's coefficient "
            "reduction (read-only here; re-import the run sheet to "
            "change them)")
        mt.addWidget(QLabel("Ref dims"), 2, 0)
        mt.addWidget(self.ref_dims_lbl, 2, 1, 1, 3)
        grid.addWidget(model_box, 3, 0, 1, 2)

        lay.addLayout(grid)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok |
                                   QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        #: set when the operator chose "OK + Set as Defaults" — the main
        #: window then stores ALL current settings (this dialog + every
        #: device's driver config) as the startup defaults
        self.defaults_requested = False
        self.defaults_btn = buttons.addButton(
            "OK + Set as Defaults",
            QDialogButtonBox.ButtonRole.AcceptRole)
        self.defaults_btn.setToolTip(
            "Apply these settings AND store the entire current state "
            "(sample rate, directories, output format + every device's "
            "ranges/rates/resolutions) as the startup defaults — "
            "auto-loaded on the next launch. Separate from Save/Load "
            "Config files.")
        self.defaults_btn.clicked.connect(
            lambda: setattr(self, "defaults_requested", True))
        lay.addWidget(buttons)

    @staticmethod
    def _ref_dims_text(config: FreestreamConfig) -> str:
        if not any((config.Sref, config.cref, config.bref)):
            return "not set — import a run sheet"
        return (f"Sref {config.Sref:g} in²   cref {config.cref:g} in   "
                f"bref {config.bref:g} in   MRC ({config.MRC_x:g}, "
                f"{config.MRC_y:g}, {config.MRC_z:g}) in")

    def _geom_spin(self, value: float, suffix: str) -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0001, 1_000_000.0)
        spin.setDecimals(4)
        spin.setValue(value)
        spin.setSuffix(suffix)
        return spin

    def _browse_root(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self, "Data root", self.data_root_edit.text())
        if path:
            self.data_root_edit.setText(path)

    def apply_to(self, config: FreestreamConfig) -> None:
        config.operator = self.operator_edit.text().strip()
        config.config_name = (self.config_name_edit.text().strip()
                              or "default")
        config.data_root = self.data_root_edit.text().strip() or "runs"
        config.sample_rate_hz = self.rate_spin.value()
        config.samples = self.samples_spin.value()
        config.dwell_s = self.dwell_spin.value()
        config.move_timeout_s = self.move_to_spin.value()
        config.output_format = str(self.format_combo.currentData() or "h5")
        config.filename_template = self.template_edit.text().strip()
        config.tunnel_timeout_s = self.tunnel_to_spin.value()
        config.mach_settle_s = self.mach_settle_spin.value()
        config.mach_tolerance = self.mach_tol_spin.value()
        config.tunnel_control_enabled = self.tunnel_ctl_chk.isChecked()
        config.mach_check_enabled = self.mach_check_chk.isChecked()
        # config.vol_path is DEVICE-OWNED (StrainBook panel → Forces tab);
        # the Forces page mirrors it into the config each tick
        config.ref_area = self.area_spin.value()
        config.ref_chord = self.chord_spin.value()
        config.ref_span = self.span_spin.value()
        config.test_name = self.test_name_edit.text().strip()
        config.model_name = self.model_name_edit.text().strip()
        config.engineer = self.engineer_edit.text().strip()
        config.data_prefix = self.prefix_edit.text().strip()
