"""Right-dock sweep planner — grid builder, run-sheet import, point table.

TEST-PARAMETER concerns only (axis specs, run-sheet import, point table);
acquisition settings (dwell, samples, sample rate…) live in ONE place —
File → Measurement Setup — and are read from the shared config here.
Nesting order is fixed at mach → beta → alpha (outermost → innermost;
``runsheet.build_grid(order=…)`` still accepts custom orders).

Axis spec syntax is the ONE sweep grammar
(:func:`freestream.sweepgrammar.expand`): ``start:delta:end`` ranges
("-4:2:8"), comma lists ("0,2,4"), a single value ("5"), trailing R for
return sweeps, and ``@named`` references; blank = axis omitted.
Double-clicking a FAILED row asks the main window to re-run just that point
(``rerunRequested``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame,
                             QGridLayout, QHBoxLayout, QHeaderView, QLabel,
                             QLineEdit, QProgressBar, QPushButton,
                             QTableWidget, QTableWidgetItem, QVBoxLayout,
                             QWidget)

from .. import speed, sweepgrammar, theme
from ..config import FreestreamConfig
from ..runbook import RunBook, RunRow, is_runbook_workbook, load_runbook
from ..runsheet import (SweepPoint, build_grid, load_runsheet,
                        points_summary)
from ..sweep import PointOutcome
from .runsheet_dialog import RunSheetDialog

_STATUS_COLOR = {
    "queued": theme.TEXT_DIM,
    "moving": theme.WARNING,
    "acquiring": theme.WARNING,
    "done": theme.SUCCESS,
    "failed": theme.ERROR,
    "skipped": theme.TEXT_DISABLED,
}

#: per-status ROW tint for the point table, so the operator can read sweep
#: progress at a glance from across the control room. Deliberately muted —
#: these sit behind text on the dark theme. None = leave the row untinted
#: (queued points stay plain, so the worked-through frontier is obvious).
_STATUS_ROW_BG = {
    "moving": "#3a3115",
    "acquiring": "#3a3115",
    "done": "#1b2a1b",
    "failed": "#3a1d1b",
    "skipped": None,
    "queued": None,
}

#: statuses that mean "this point is being worked right now" — the row the
#: table auto-scrolls to keep in view.
_ACTIVE_STATUSES = ("moving", "acquiring")

#: axis-set definitions per positioner family: (field, row label, hint).
#: "aero" = attitude sweeps (crescent/ate + tunnel); "xyz" = the Mode-3
#: traverse position matrix (x innermost — see runsheet.DEFAULT_ORDER).
#: Both sets carry the speed row ("mach" — nested OUTERMOST by
#: runsheet.DEFAULT_ORDER), so a traverse flow survey can step the
#: tunnel speed too; its label/hint are re-skinned per the configured
#: speed unit in _populate_axis_rows.
_AXIS_SETS = {
    "aero": (
        ("alpha", "alpha [deg]", "e.g. -4:2:8  (start:delta:end)"),
        ("beta", "beta [deg]", "e.g. 0  or  -2,0,2"),
        ("mach", "mach", "e.g. 0.3  or  0.3,0.5,0.7  (air-off 0 added)"),
    ),
    "xyz": (
        ("x", "X [in]", "e.g. 0:0.5:12  (start:delta:end)"),
        ("y", "Y [in]", "e.g. 0:1:6  (blank = omit)"),
        ("z", "Z [in]", "e.g. 0  or  0,3,6"),
        ("mach", "mach", "e.g. 0.3  or  0.3,0.5,0.7  (blank = omit)"),
    ),
}

#: compact axis symbols for the run-book indicator strip
_AXIS_SYMBOL = {"alpha": "α", "beta": "β", "mach": "M",
                "x": "X", "y": "Y", "z": "Z"}


class PlannerPanel(QWidget):
    rerunRequested = pyqtSignal(int)          # table row == point index
    message = pyqtSignal(str)                 # console lines
    runApplied = pyqtSignal()                 # a run sheet was loaded/applied

    def __init__(self, config: FreestreamConfig, parent=None):
        super().__init__(parent)
        self.config = config          # dwell/samples come from HERE
        self.points: List[SweepPoint] = []
        self.axis_mode = "aero"
        #: True while the main window runs a sweep — Clear Grid is locked
        self._sweep_running = False
        self._axis_edits: dict = {}   # field name → QLineEdit
        self._axis_labels: dict = {}  # field name → QLabel (row labels)
        #: loaded run-book context (None until a run sheet is imported)
        self._runbook: Optional[RunBook] = None
        self._run_row: Optional[RunRow] = None
        self._named: Dict[str, str] = {}
        #: row -> last painted status, so the 250 ms poll only repaints rows
        #: that actually changed (a 500-point grid otherwise restyles 2500
        #: cells four times a second)
        self._row_status: Dict[int, str] = {}
        #: auto-scroll the table to the point being acquired. Paused when the
        #: operator scrolls away, resumed when they scroll back to the end.
        self._auto_follow = True
        self._programmatic_scroll = False
        self._last_scrolled_row: Optional[int] = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(5)

        # ── destination strip: WHERE this sweep gets written. Top of the
        #    pane, above the run-book/expansion summary, because "which
        #    folder am I about to fill" is the first thing to check when a
        #    test starts. ──────────────────────────────────────────────────
        self.dest_lbl = QLabel()
        self.dest_lbl.setObjectName("plannerDest")
        # exactly two lines by construction, and the path is pre-elided to
        # fit — wrapping would only let the strip grow and eat table space
        self.dest_lbl.setWordWrap(False)
        self.dest_lbl.setStyleSheet(
            f"QLabel#plannerDest {{ background: {theme.BG_LIGHT}; "
            f"border: 1px solid {theme.BORDER}; border-radius: 4px; "
            f"padding: 4px 8px; font-family: 'Consolas', monospace; }}")
        lay.addWidget(self.dest_lbl)

        # ── run-book indicator strip (below the "Sweep Planner" title,
        #    above the alpha/beta/mach fields) ─────────────────────────────
        self.indicator = QLabel()
        self.indicator.setObjectName("runIndicator")
        self.indicator.setWordWrap(True)
        self.indicator.setFrameShape(QFrame.Shape.StyledPanel)
        self.indicator.setStyleSheet(
            f"QLabel#runIndicator {{ background: {theme.BG_LIGHTER}; "
            f"border: 1px solid {theme.BORDER}; border-radius: 4px; "
            f"padding: 6px 8px; font-family: 'Consolas', monospace; }}")
        lay.addWidget(self.indicator)

        self._axis_grid = QGridLayout()
        self._axis_grid.setHorizontalSpacing(6)
        self._axis_grid.setVerticalSpacing(4)
        self._populate_axis_rows()
        lay.addLayout(self._axis_grid)

        # dwell/samples/sample-rate are set ONCE in File → Measurement
        # Setup; grids inherit them from the shared config on Build.
        acq_note = QLabel("dwell / samples: File → Measurement Setup…")
        acq_note.setObjectName("dim")
        acq_note.setToolTip("Acquisition settings (dwell, samples per "
                            "point, sample rate) are suite-wide and edited "
                            "in the Measurement Setup dialog.")
        lay.addWidget(acq_note)

        btns = QHBoxLayout()
        self.build_btn = QPushButton("Build Grid")
        self.build_btn.setObjectName("primary")
        self.build_btn.clicked.connect(self._build_clicked)
        self.import_btn = QPushButton("Import Run Sheet…")
        self.import_btn.clicked.connect(self._import_clicked)
        btns.addWidget(self.build_btn)
        btns.addWidget(self.import_btn)
        lay.addLayout(btns)

        self.table = QTableWidget(0, len(self._table_cols()))
        self.table.setHorizontalHeaderLabels(self._table_cols())
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setStretchLastSection(True)
        # tighten the HORIZONTAL padding: the suite-wide header rule is a
        # uniform 6 px, which on a five-column numeric table wastes 12 px per
        # column and squeezes the labels. Trim the sides only — row height,
        # fonts and grid lines stay as the theme sets them.
        self.table.setStyleSheet(
            "QHeaderView::section { padding: 6px 2px; }"
            "QTableWidget::item { padding: 0px 2px; }")
        # size each column to its content (header included, so nothing
        # clips) and let the status column take up the slack
        header = self.table.horizontalHeader()
        for col in range(self.table.columnCount() - 1):
            header.setSectionResizeMode(
                col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.cellDoubleClicked.connect(self._double_clicked)
        self.table.verticalScrollBar().valueChanged.connect(
            self._on_table_scrolled)
        lay.addWidget(self.table, stretch=1)

        self.progress = QProgressBar()
        self.progress.setTextVisible(True)
        self.progress.setFormat("%v / %m points")
        # the sweep progress bar is read from across the control room —
        # give it real presence rather than the default hairline
        self.progress.setMinimumHeight(26)
        _pf = self.progress.font()
        _pf.setPointSize(_pf.pointSize() + 1)
        _pf.setBold(True)
        self.progress.setFont(_pf)
        lay.addWidget(self.progress)
        self.summary_lbl = QLabel("0 points")
        self.summary_lbl.setObjectName("dim")
        self.summary_lbl.setWordWrap(True)
        lay.addWidget(self.summary_lbl)

        # engine mutates SweepPoint.status in-place on a worker thread;
        # a light poll keeps the colors live regardless of callback order
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self.refresh_statuses)
        self._timer.start()

        self._update_indicator()

    def set_config(self, config: FreestreamConfig) -> None:
        """Adopt a freshly loaded config (source of dwell/samples).
        The loaded config may carry a different speed unit — refresh
        the unit-facing chrome so the speed row matches it."""
        self.config = config
        self._refresh_speed_unit()

    # ── speed units (freestream.speed; canonical axis stays Mach) ────────
    def _speed_unit(self) -> str:
        """The configured entry/display unit, defended to a known one."""
        unit = getattr(self.config, "speed_unit", "mach")
        return unit if unit in speed.SPEED_UNITS else "mach"

    def set_speed_unit(self, unit: str) -> None:
        """Measurement Setup accepted a (possibly new) speed unit:
        adopt it on the SHARED config and refresh the unit-facing
        chrome (speed-row label + placeholder, table header, indicator
        symbol). Planned points are kept — their canonical mach and
        stamped entered-unit meta stay valid."""
        if unit not in speed.SPEED_UNITS:
            unit = "mach"
        self.config.speed_unit = unit
        self._refresh_speed_unit()

    def _speed_row_label(self) -> str:
        return f"speed [{speed.LABELS[self._speed_unit()]}]"

    def _speed_row_hint(self) -> str:
        """The speed-row placeholder for the ACTIVE axis mode: the aero
        set keeps speed.PLANNER_HINTS verbatim; the xyz (traverse) set
        drops the "(air-off 0 added)" note — the traverse planner does
        NOT auto-prepend the air-off point (see _build_clicked)."""
        hint = speed.PLANNER_HINTS[self._speed_unit()]
        if self.axis_mode != "aero":
            hint = (hint.replace("; air-off 0 added", "")
                        .replace("  (air-off 0 added)", ""))
        return hint

    def _refresh_speed_unit(self) -> None:
        """Re-skin everything that names the speed unit (no rebuild).
        Both axis modes carry the speed row now, so the label/placeholder
        refresh is mode-agnostic (the .get() guards cover axis sets
        without one)."""
        lbl = self._axis_labels.get("mach")
        if lbl is not None:
            lbl.setText(self._speed_row_label())
        edit = self._axis_edits.get("mach")
        if edit is not None:
            edit.setPlaceholderText(self._speed_row_hint())
        self._reshape_table_columns()
        self._update_indicator()

    # legacy accessors (pre-axis-mode API); only valid in "aero" mode
    @property
    def alpha_edit(self) -> QLineEdit:
        return self._axis_edits["alpha"]

    @property
    def beta_edit(self) -> QLineEdit:
        return self._axis_edits["beta"]

    @property
    def mach_edit(self) -> QLineEdit:
        return self._axis_edits["mach"]

    # ── axis modes (aero α/β/mach vs traverse X/Y/Z) ─────────────────────
    def _axis_fields(self):
        return _AXIS_SETS[self.axis_mode]

    def _table_cols(self):
        cols = []
        for f in self._axis_fields():
            name = f[0]
            # the mach column header shows the ENTERED unit (the cells
            # display what the operator typed); canonical mach keeps
            # the historical plain header. Applies in BOTH axis modes —
            # the xyz set carries the speed row too.
            if name == "mach" and self._speed_unit() != "mach":
                name = self._speed_row_label()
            cols.append(name)
        return ("#", *cols, "status")

    def _reshape_table_columns(self) -> None:
        """(Re)shape the point table to the active axis set: column count
        (aero = 5, xyz = 6 — the traverse set carries a speed column),
        header labels, and per-column sizing (content-sized parameters,
        the status column takes the slack via the stretchy last
        section)."""
        cols = self._table_cols()
        if self.table.columnCount() != len(cols):
            self.table.setColumnCount(len(cols))
            header = self.table.horizontalHeader()
            for col in range(len(cols) - 1):
                header.setSectionResizeMode(
                    col, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setHorizontalHeaderLabels(cols)

    def _populate_axis_rows(self) -> None:
        while self._axis_grid.count():
            item = self._axis_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._axis_edits.clear()
        self._axis_labels.clear()
        for row, (field_name, label, hint) in enumerate(self._axis_fields()):
            if field_name == "mach":
                # the tunnel row speaks the CONFIGURED speed unit
                # (freestream.speed) in BOTH axis modes; the canonical
                # axis stays Mach (xyz hints drop the air-off-0 note)
                label = self._speed_row_label()
                hint = self._speed_row_hint()
            edit = QLineEdit()
            edit.setPlaceholderText(hint)
            edit.setToolTip("Axis vector: start:delta:end range (the MIDDLE "
                            "value is the step), a comma list, a single "
                            "value, @named reference, or trailing R = return "
                            "sweep (hysteresis). Blank = axis omitted.")
            edit.textChanged.connect(self._update_indicator)
            self._axis_edits[field_name] = edit
            lbl = QLabel(label)
            self._axis_labels[field_name] = lbl
            self._axis_grid.addWidget(lbl, row, 0)
            self._axis_grid.addWidget(edit, row, 1)

    def set_axis_mode(self, mode: str) -> None:
        """Switch the planner between attitude sweeps ("aero") and the
        Mode-3 traverse position matrix ("xyz"). Clears any planned
        points (they were built for the other axis set)."""
        if mode not in _AXIS_SETS or mode == self.axis_mode:
            return
        self.axis_mode = mode
        # leaving the run-book axis set drops the loaded-run context
        self._runbook = None
        self._run_row = None
        self._named = {}
        self._populate_axis_rows()
        self._reshape_table_columns()
        self.set_points([])
        self._update_indicator()
        self.message.emit(
            "sweep planner axes → "
            + " / ".join(f[0] for f in self._axis_fields())
            + ("  (traverse position matrix; x varies fastest)"
               if mode == "xyz" else "  (attitude sweep)"))

    # ── building / importing ─────────────────────────────────────────────
    @staticmethod
    def _spec(edit: QLineEdit) -> Optional[str]:
        text = edit.text().strip()
        return text or None

    def _build_clicked(self) -> None:
        # toggle: once a grid exists (Build Grid OR a run-sheet load) the
        # button reads "Clear Grid" and clicking it resets the planner
        if self.points:
            self.clear_grid()
            return
        # nesting order from runsheet.DEFAULT_ORDER (mach → beta → alpha,
        # z → y → x innermost); dwell/samples from the ONE config. In aero
        # mode the mach axis gets the auto-prepended air-off 0 (workbook
        # grammar), so a manual Build Grid matches what a run sheet would.
        # The xyz (traverse) set does NOT auto-prepend it: the air-off
        # pass exists for BALANCE weight tares as the model attitude
        # changes; a flow survey moves a probe, not the model, and speed
        # nests OUTERMOST — a forced 0 would prepend the ENTIRE spatial
        # matrix at air-off. An operator who wants a tare pass types the
        # 0 explicitly. Speed-unit conversion + entered-value meta
        # stamping are identical in both modes.
        specs: dict = {}
        unit = self._speed_unit()
        value_by_mach = None          # canonical mach → entered value
        for name, edit in self._axis_edits.items():
            spec = self._spec(edit)
            if name == "mach" and spec:
                try:
                    entered = sweepgrammar.expand(
                        spec, named=self._named,
                        ensure_zero_for_mach=(self.axis_mode == "aero"))
                except sweepgrammar.GrammarError as exc:
                    self.message.emit(f"grid build failed: {exc}")
                    return
                if unit == "mach":
                    spec = entered
                else:
                    # the operator typed the ENTERED unit — convert to
                    # the canonical Mach axis (speed.py nominal maps),
                    # remembering the typed values for display/metadata.
                    # 0 stays 0 in every unit, so the auto-prepended
                    # air-off point survives the conversion.
                    try:
                        spec = [speed.mach_from(v, unit,
                                                self.config.rpm_per_mach)
                                for v in entered]
                    except ValueError as exc:
                        self.message.emit(f"grid build failed: {exc}")
                        return
                    value_by_mach = dict(
                        zip(spec, (float(v) for v in entered)))
            specs[f"{name}_spec"] = spec
        try:
            points = build_grid(dwell_s=self.config.dwell_s,
                                samples=self.config.samples,
                                named=self._named or None, **specs)
        except ValueError as exc:
            self.message.emit(f"grid build failed: {exc}")
            return
        if value_by_mach is not None:
            self._stamp_speed_meta(points, unit, value_by_mach)
        for i, p in enumerate(points):     # row_index == table row for grids
            p.row_index = i
        self.set_points(points)
        self._update_indicator()
        self.message.emit("grid built: " + points_summary(points))

    @staticmethod
    def _stamp_speed_meta(points, unit: str, value_by_mach: dict) -> None:
        """Non-mach entry units: every built point keeps the value the
        operator TYPED (meta["speed_value"]/["speed_unit"]) next to the
        canonical SweepPoint.mach — the engine/dialog display it and
        the recorder stamps it into the file attrs. The rpm unit
        additionally rides the engine's documented direct-RPM override
        (meta["rpm"]) so the fan is commanded verbatim."""
        for p in points:
            if p.mach is None:
                continue
            value = value_by_mach.get(p.mach)
            if value is None:                # defensive; never expected
                continue
            p.meta["speed_value"] = value
            p.meta["speed_unit"] = unit
            if unit == "rpm":
                p.meta["rpm"] = value

    def clear_grid(self) -> None:
        """Full sweep-planner reset (the Clear Grid side of the toggle):
        empty the point table, drop the loaded run-book context (indicator
        back to its placeholder), blank the axis-spec fields and reset the
        progress bar/summary. Measurement-settings values a run sheet
        merged into the shared CONFIG (test info, ref dims, config name)
        are deliberately KEPT — they live in Measurement Setup now."""
        if self._sweep_running:
            self.message.emit("clear grid refused — a sweep is running")
            return
        self._runbook = None
        self._run_row = None
        self._named = {}
        for edit in self._axis_edits.values():
            edit.blockSignals(True)
            edit.clear()
            edit.blockSignals(False)
        self.set_points([])                # table/progress/summary + button
        self._update_indicator()           # placeholder text
        self.message.emit("grid cleared — sweep planner reset")

    def set_sweep_running(self, running: bool) -> None:
        """Main-window hook: lock Clear Grid while the plan executes."""
        if running == self._sweep_running:
            return
        self._sweep_running = running
        if running:
            # a new run re-arms auto-follow even if the operator had
            # scrolled away while reviewing the previous one
            self._auto_follow = True
            self._last_scrolled_row = None
        self._sync_grid_button()

    def _sync_grid_button(self) -> None:
        """Build Grid ⇄ Clear Grid toggle follows the planner state."""
        has_grid = bool(self.points)
        self.build_btn.setText("Clear Grid" if has_grid else "Build Grid")
        self.build_btn.setObjectName("danger" if has_grid else "primary")
        style = self.build_btn.style()     # re-apply the stylesheet rule
        style.unpolish(self.build_btn)
        style.polish(self.build_btn)
        self.build_btn.setToolTip(
            "Clear the planned grid — resets the point table, axis specs "
            "and any loaded run sheet" if has_grid
            else "Expand the axis specs into a test-point grid")
        # a grid that is being executed cannot be cleared
        self.build_btn.setEnabled(not (has_grid and self._sweep_running))

    def _import_clicked(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Import Run Sheet", "",
            "Run sheets (*.xlsx *.xlsm *.csv);;All files (*)")
        if not path:
            return
        # PRIMARY path: the 5-sheet run-sheet WORKBOOK → selection dialog
        if is_runbook_workbook(path):
            self._import_workbook(path)
            return
        # FALLBACK: a flat single-sheet CSV/XLSX run sheet (legacy)
        try:
            points = load_runsheet(path)
        except Exception as exc:                       # noqa: BLE001
            self.message.emit(f"run-sheet import failed: {exc}")
            return
        # a pure x/y/z sheet flips the planner into traverse-matrix mode
        # (and vice versa) so the table columns match the sheet's axes
        has_xyz = any(p.x is not None or p.y is not None or p.z is not None
                      for p in points)
        has_aero = any(p.alpha is not None or p.beta is not None
                       or p.mach is not None for p in points)
        if has_xyz and not has_aero:
            self.set_axis_mode("xyz")
        elif has_aero and not has_xyz:
            self.set_axis_mode("aero")
        self.set_points(points)
        self._update_indicator()
        self.message.emit(f"imported {path}: " + points_summary(points))

    def _import_workbook(self, path: str) -> None:
        try:
            runbook = load_runbook(path)
        except Exception as exc:                       # noqa: BLE001
            self.message.emit(f"run sheet load failed: {exc}")
            return
        dialog = RunSheetDialog(runbook, self)
        if not dialog.exec():
            self.message.emit("run-sheet import cancelled")
            return
        run_row, points = dialog.result_points()
        if not points:
            self.message.emit("run-sheet import: no points selected")
            return
        self.apply_run_selection(runbook, run_row, points)
        label = run_row.run if run_row is not None else "all enabled runs"
        self.message.emit(
            f"run sheet '{path}' → {label}: " + points_summary(points))

    # ── applying a run-sheet selection ───────────────────────────────────
    def apply_run_selection(self, runbook: RunBook,
                            run_row: Optional[RunRow],
                            points: List[SweepPoint]) -> None:
        """Adopt a run selected from the import dialog (§5).

        Populates the point table, fills the axis line-edits with the run's
        cells, applies the run's samples/sample-rate + the run book's
        test-info / reference dimensions into the shared config, and emits
        :attr:`runApplied` so the main window can refresh the recorder.
        """
        self._runbook = runbook
        self._run_row = run_row
        self._named = dict(runbook.named_arrays)
        # run sheets are attitude sweeps
        if self.axis_mode != "aero":
            self.axis_mode = "aero"
            self._populate_axis_rows()
            self._reshape_table_columns()
        # fill the axis fields with the run's cells (operator can tweak)
        if run_row is not None:
            for name, cell in (("alpha", run_row.alpha_cell),
                               ("beta", run_row.beta_cell),
                               ("mach", run_row.mach_cell)):
                edit = self._axis_edits.get(name)
                if edit is not None:
                    edit.blockSignals(True)
                    edit.setText(cell)
                    edit.blockSignals(False)
            # the run's acquisition OVERRIDES the global defaults
            if run_row.samples is not None:
                self.config.samples = int(run_row.samples)
            if run_row.sample_rate_hz is not None:
                self.config.sample_rate_hz = float(run_row.sample_rate_hz)
        self._merge_runbook_into_config(runbook)
        # the run row's Model Config NAME becomes the measurement config
        # name (folder-per-configuration under the data root)
        if run_row is not None and run_row.config_name:
            if run_row.config_name != self.config.config_name:
                self.message.emit(
                    f"config name → '{run_row.config_name}' (inherited "
                    f"from the run sheet; was "
                    f"'{self.config.config_name or '—'}')")
            self.config.config_name = run_row.config_name
        for i, p in enumerate(points):     # row_index == table row
            p.row_index = i
        self.set_points(points)
        self._update_indicator()
        self.runApplied.emit()

    def _merge_runbook_into_config(self, runbook: RunBook) -> None:
        """Fold the run book's test-info + reference dims into the config so
        they land in recorded metadata (§5c)."""
        info = runbook.friendly_info()
        cfg = self.config
        text_fields = (("test_name", "test_name"), ("model_name",
                       "model_name"), ("facility", "facility"),
                       ("engineer", "engineer"), ("operator", "operator"),
                       ("data_prefix", "data_prefix"),
                       ("objectives", "objectives"))
        for cfg_attr, info_key in text_fields:
            value = info.get(info_key)
            if value not in (None, ""):
                old = getattr(cfg, cfg_attr, "")
                new = str(value)
                # inherit ALWAYS (a run sheet is the source of truth for
                # its test), but log when it replaces a manual entry
                if cfg_attr == "operator" and old and old != new:
                    self.message.emit(f"operator → '{new}' (inherited from "
                                      f"the run sheet; was '{old}')")
                setattr(cfg, cfg_attr, new)
        # reference dimensions (feed Streamlined coefficient reduction)
        for symbol in ("Sref", "cref", "bref", "MRC_x", "MRC_y", "MRC_z"):
            value = runbook.ref_dim_value(symbol)
            if value is not None:
                setattr(cfg, symbol, value)
        # mirror S/c/b into the balance-reduction fields too
        for symbol, mirror in (("Sref", "ref_area"), ("cref", "ref_chord"),
                               ("bref", "ref_span")):
            value = runbook.ref_dim_value(symbol)
            if value is not None:
                setattr(cfg, mirror, value)

    # ── run-book indicator strip ─────────────────────────────────────────
    def _indicator_style(self, color: str) -> str:
        return (f"QLabel#runIndicator {{ background: {theme.BG_LIGHTER}; "
                f"border: 1px solid {theme.BORDER}; border-radius: 4px; "
                f"padding: 6px 8px; font-family: 'Consolas', monospace; "
                f"color: {color}; }}")

    @staticmethod
    def _elide_path(path: str, limit: int = 44) -> str:
        """Trim a long output path from the FRONT, on a separator, so the
        strip stays two lines. The tail is what distinguishes one run folder
        from another; the full path lives in the tooltip."""
        if len(path) <= limit:
            return path
        parts = path.replace("\\", "/").split("/")
        kept: List[str] = []
        for part in reversed(parts):
            candidate = "/".join([part] + kept)
            if kept and len(candidate) > limit - 2:
                break
            kept.insert(0, part)
        return "…/" + "/".join(kept) if kept else path[-limit:]

    def _update_destination(self) -> None:
        """Refresh the destination strip: the config name and the folder the
        recorder will write into (``data_root`` / ``config_name`` — the same
        join Hdf5Recorder makes). Shown verbatim as configured rather than
        resolved, because a relative root resolves against the working
        directory, not the app."""
        cfg_name = str(getattr(self.config, "config_name", "") or "") or "—"
        root = str(getattr(self.config, "data_root", "") or "")
        try:
            out_dir = str(Path(root) / cfg_name) if root else cfg_name
        except (TypeError, ValueError):     # pathological config values
            out_dir = cfg_name
        self.dest_lbl.setText(
            f"<span style='color:{theme.TEXT_DIM}'>config&nbsp;&nbsp;</span>"
            f"<span style='color:{theme.TEXT}'><b>{cfg_name}</b></span><br>"
            f"<span style='color:{theme.TEXT_DIM}'>writes&nbsp;&nbsp;</span>"
            f"<span style='color:{theme.TEXT_DIM}'>"
            f"{self._elide_path(out_dir)}</span>")
        self.dest_lbl.setToolTip(
            f"Run files are written to:\n  {out_dir}\n\n"
            "Data root and config name are set in "
            "File → Measurement Setup…")

    def _update_indicator(self) -> None:
        self._update_destination()
        summary = self._expansion_summary()
        if summary is None:
            self.indicator.setStyleSheet(
                self._indicator_style(theme.TEXT_DISABLED))
            self.indicator.setText(
                "no run sheet loaded — build a grid manually or import a "
                "run sheet")
            return
        color = (theme.ERROR if summary.startswith("invalid ")
                 else theme.TEXT)
        self.indicator.setStyleSheet(self._indicator_style(color))
        self.indicator.setText(summary)

    def _expansion_summary(self) -> Optional[str]:
        """Build the indicator string from the current axis cells (live)."""
        # "all enabled runs" convenience: a concatenated grid, not a single
        # editable run — summarize the whole book instead of the axis cells.
        if self._runbook is not None and self._run_row is None:
            return f"all enabled runs · {len(self.points)} pts"
        fields = self._axis_fields()
        names = [f[0] for f in fields]
        cells = {n: self._axis_edits[n].text().strip() for n in names}
        if all(not cells[n] for n in names) and self._run_row is None:
            return None
        parts: List[str] = []
        total = 1
        # display outermost → innermost (reverse of the inner-first fields)
        for name in reversed(names):
            cell = cells[name]
            is_mach = name == "mach"
            try:
                # the air-off 0 is only auto-added on the aero set (see
                # _build_clicked) — the indicator's point count must
                # match what Build Grid will actually produce
                vals = sweepgrammar.expand(
                    cell, named=self._named,
                    ensure_zero_for_mach=(
                        is_mach and self.axis_mode == "aero")) \
                    if cell else []
            except sweepgrammar.GrammarError:
                return f"invalid {name} cell: {cell!r}"
            symbol = _AXIS_SYMBOL.get(name, name)
            if is_mach:
                # the tunnel-axis symbol follows the entry unit
                # (M / V / N — freestream.speed) in both axis modes
                symbol = speed.AXIS_SYMBOLS.get(self._speed_unit(), "M")
            if is_mach and vals:
                shown = ", ".join(f"{v:g}" for v in vals)
            else:
                shown = cell or "—"
            parts.append(f"{symbol}[{shown}]")
            if vals:
                total *= len(vals)
        body = " × ".join(parts)
        prefix = ""
        if self._run_row is not None:
            prefix = (f"{self._run_row.run} · config: "
                      f"{self._run_row.config_name or '—'} · ")
        return f"{prefix}{body} · {total} pts"

    # ── table state ──────────────────────────────────────────────────────
    def set_points(self, points: List[SweepPoint]) -> None:
        self.points = points
        self.table.setRowCount(len(points))
        axis_names = [f[0] for f in self._axis_fields()]
        for row, p in enumerate(points):
            values = [row]
            for n in axis_names:
                val = getattr(p, n)
                # the speed column shows the ENTERED-unit value the
                # operator typed (header names the unit); the canonical
                # mach still drives the sweep underneath
                if n == "mach" and "speed_value" in p.meta:
                    val = p.meta["speed_value"]
                values.append(val)
            values.append(p.status)
            for col, val in enumerate(values):
                text = "—" if val is None else (
                    f"{val:g}" if isinstance(val, float) else str(val))
                item = QTableWidgetItem(text)
                item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                self.table.setItem(row, col, item)
        # a fresh table has nothing painted yet — drop the repaint cache so
        # refresh_statuses styles every row once
        self._row_status.clear()
        self._last_scrolled_row = None
        self._auto_follow = True
        self.progress.setMaximum(max(len(points), 1))
        self.progress.setValue(0)
        self.summary_lbl.setText(points_summary(points))
        self._sync_grid_button()
        self.refresh_statuses()

    def _style_row(self, row: int, status: str) -> None:
        """Tint a whole row for its status — the point being acquired is
        highlighted, completed points read as done, failures stand out."""
        bg = _STATUS_ROW_BG.get(status)
        status_fg = QColor(_STATUS_COLOR.get(status, theme.TEXT))
        active = status in _ACTIVE_STATUSES
        last_col = self.table.columnCount() - 1
        for col in range(self.table.columnCount()):
            item = self.table.item(row, col)
            if item is None:
                continue
            if bg is None:
                # clear any tint from a previous status (a re-run point
                # goes failed -> queued -> acquiring)
                item.setData(Qt.ItemDataRole.BackgroundRole, None)
            else:
                item.setBackground(QColor(bg))
            # only the status cell carries the status color; the parameter
            # cells stay plainly readable against the tint
            item.setForeground(status_fg if col == last_col
                               else QColor(theme.TEXT))
            font = item.font()
            font.setBold(active)
            item.setFont(font)

    def _on_table_scrolled(self, value: int) -> None:
        """Operator scrolling pauses auto-follow; scrolling back to the
        bottom resumes it (the usual log-tail behaviour)."""
        if self._programmatic_scroll:
            return
        self._auto_follow = value >= self.table.verticalScrollBar().maximum()

    def _follow_active_row(self, active_row: Optional[int],
                           finished: int) -> None:
        """Keep the point under acquisition in view as the sweep advances."""
        if not self._auto_follow or not self.points:
            return
        row = active_row
        if row is None:
            # between points: sit on the frontier of completed work, but
            # only while a sweep is actually running — otherwise building a
            # grid would yank the view around
            if not self._sweep_running:
                return
            row = min(finished, len(self.points) - 1)
        if row < 0 or row == self._last_scrolled_row:
            return
        item = self.table.item(row, 0)
        if item is None:
            return
        self._last_scrolled_row = row
        self._programmatic_scroll = True
        try:
            self.table.scrollToItem(
                item, QAbstractItemView.ScrollHint.PositionAtCenter)
        finally:
            self._programmatic_scroll = False

    def refresh_statuses(self) -> None:
        if not self.points or self.table.rowCount() != len(self.points):
            return
        status_col = self.table.columnCount() - 1
        finished = 0
        active_row: Optional[int] = None
        for row, p in enumerate(self.points):
            if p.status in ("done", "failed", "skipped"):
                finished += 1
            elif p.status in _ACTIVE_STATUSES and active_row is None:
                active_row = row
            if self._row_status.get(row) == p.status:
                continue                   # unchanged — skip the repaint
            self._row_status[row] = p.status
            item = self.table.item(row, status_col)
            if item is not None:
                item.setText(p.status)
            self._style_row(row, p.status)
        self.progress.setValue(finished)
        self._follow_active_row(active_row, finished)

    def mark_done(self, outcome: PointOutcome) -> None:
        self.refresh_statuses()
        if outcome.status == "failed":
            self.message.emit(f"point {outcome.index} failed: "
                              f"{outcome.error} (double-click the row to "
                              "re-run it)")

    def _double_clicked(self, row: int, _col: int) -> None:
        if 0 <= row < len(self.points) and \
                self.points[row].status == "failed":
            self.rerunRequested.emit(row)

    def shutdown(self) -> None:
        self._timer.stop()
