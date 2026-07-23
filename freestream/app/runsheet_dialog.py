"""Run-sheet import dialog — pick a run from a loaded workbook (§5.4).

Shows the parsed :class:`freestream.runbook.RunBook`: a Test-Info summary
(facility / model / engineer / dates / objectives + the model reference
dimensions table), the Run Matrix with a LIVE expanded-point count per row
(disabled rows dimmed), and small Model Configs + Named Arrays views.  The
operator selects one run row (the primary flow) or chooses "All enabled runs";
OK returns the chosen :class:`~freestream.runbook.RunRow`(s) and the expanded
:class:`~freestream.runsheet.SweepPoint` matrix.

Dark-themed via :mod:`freestream.theme`; no Qt state leaks back into the
run book.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (QAbstractItemView, QDialog, QDialogButtonBox,
                             QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                             QPushButton, QScrollArea, QSplitter,
                             QTableWidget, QTableWidgetItem, QTabWidget,
                             QVBoxLayout, QWidget)

from .. import theme
from ..runbook import RunBook, RunRow, build_run_points, expanded_count
from ..runsheet import SweepPoint

_MATRIX_COLS = ("run", "on", "alpha", "beta", "mach", "# pts", "config",
                "notes")


def _dash(value) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return "—"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


class RunSheetDialog(QDialog):
    """Modal run-sheet selection dialog."""

    def __init__(self, runbook: RunBook, parent=None):
        super().__init__(parent)
        self.runbook = runbook
        self.selected_run: Optional[RunRow] = None
        self.select_all_enabled = False

        name = runbook.source_path.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
        self.setWindowTitle(f"Import Run Sheet — {name}")
        self.setMinimumSize(1000, 780)
        # large content → real min/max buttons (maximizable)
        self.setWindowFlags(self.windowFlags()
                            | Qt.WindowType.WindowMinMaxButtonsHint)
        self.setStyleSheet(theme.get_stylesheet())

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        title = QLabel(f"Run sheet: {name}")
        title.setStyleSheet(
            f"color: {theme.ACCENT_LIGHT}; font-size: 13pt; font-weight: bold;")
        root.addWidget(title)

        # ── upper: Test Info (left) | Run Matrix (right) ─────────────────
        upper = QSplitter(Qt.Orientation.Horizontal)
        info_scroll = QScrollArea()
        info_scroll.setWidgetResizable(True)
        info_scroll.setWidget(self._build_test_info_panel())
        info_scroll.setMinimumWidth(320)
        upper.addWidget(info_scroll)
        upper.addWidget(self._build_matrix_panel())
        upper.setStretchFactor(0, 0)
        upper.setStretchFactor(1, 1)
        upper.setSizes([340, 660])
        root.addWidget(upper, stretch=5)

        # ── lower: Model Configs / Named Arrays tabs ─────────────────────
        tabs = QTabWidget()
        tabs.addTab(self._build_configs_view(), "Model Configs")
        tabs.addTab(self._build_named_view(), "Named Arrays")
        tabs.setMaximumHeight(200)
        root.addWidget(tabs, stretch=1)

        # ── buttons ───────────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self.all_btn = QPushButton("Load all enabled runs")
        self.all_btn.setToolTip(
            "Concatenate every enabled run's expanded points into one grid.")
        self.all_btn.clicked.connect(self._accept_all_enabled)
        btn_row.addWidget(self.all_btn)
        btn_row.addStretch(1)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self.ok_btn = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        self.ok_btn.setText("Load selected run")
        self.buttons.accepted.connect(self._accept_selected)
        self.buttons.rejected.connect(self.reject)
        btn_row.addWidget(self.buttons)
        root.addLayout(btn_row)

        self._select_first_enabled()

    # ── panels ────────────────────────────────────────────────────────────
    def _build_test_info_panel(self) -> QWidget:
        info = self.runbook.friendly_info()
        box = QGroupBox("Test Info")
        outer = QVBoxLayout(box)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        rows = (("Facility", "facility"), ("Model", "model_name"),
                ("Engineer", "engineer"), ("Operator(s)", "operator"),
                ("Start", "start_date"), ("End", "end_date"),
                ("Nominal Mach", "nominal_mach"), ("Data prefix",
                                                   "data_prefix"))
        for label, key in rows:
            val = QLabel(_dash(info.get(key)))
            val.setWordWrap(True)
            form.addRow(QLabel(label), val)
        obj = QLabel(_dash(info.get("objectives")))
        obj.setWordWrap(True)
        obj.setStyleSheet(f"color: {theme.TEXT_DIM};")
        form.addRow(QLabel("Objectives"), obj)
        outer.addLayout(form)

        outer.addWidget(self._section_label("Model reference dimensions"))
        ref = QTableWidget(0, 3)
        ref.setHorizontalHeaderLabels(("symbol", "value", "units"))
        ref.verticalHeader().setVisible(False)
        ref.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        ref.horizontalHeader().setStretchLastSection(True)
        for symbol, entry in self.runbook.ref_dims.items():
            r = ref.rowCount()
            ref.insertRow(r)
            ref.setItem(r, 0, QTableWidgetItem(str(symbol)))
            ref.setItem(r, 1, QTableWidgetItem(_dash(entry.get("value"))))
            ref.setItem(r, 2, QTableWidgetItem(_dash(entry.get("units"))))
        ref.resizeColumnsToContents()
        ref.resizeRowsToContents()
        # show every reference-dimension row without needing to scroll
        row_h = ref.verticalHeader().defaultSectionSize()
        header_h = ref.horizontalHeader().height()
        ref.setMinimumHeight(header_h + row_h * max(ref.rowCount(), 1) + 8)
        outer.addWidget(ref)
        outer.addStretch(1)
        return box

    def _build_matrix_panel(self) -> QWidget:
        box = QGroupBox("Run Matrix — select a run")
        lay = QVBoxLayout(box)
        self.matrix = QTableWidget(len(self.runbook.runs), len(_MATRIX_COLS))
        self.matrix.setHorizontalHeaderLabels(_MATRIX_COLS)
        self.matrix.verticalHeader().setVisible(False)
        self.matrix.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers)
        self.matrix.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.matrix.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection)
        self.matrix.horizontalHeader().setStretchLastSection(True)
        for row, run in enumerate(self.runbook.runs):
            try:
                n_pts = expanded_count(self.runbook, run)
            except Exception:                              # noqa: BLE001
                n_pts = 0
            values = (run.run, "Y" if run.enable else "N", run.alpha_cell,
                      run.beta_cell, run.mach_cell, str(n_pts),
                      run.config_name, run.notes)
            for col, val in enumerate(values):
                item = QTableWidgetItem(_dash(val))
                if col in (1, 5):
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if not run.enable:                         # dim disabled rows
                    item.setForeground(QColor(theme.TEXT_DISABLED))
                self.matrix.setItem(row, col, item)
        self.matrix.resizeColumnsToContents()
        self.matrix.itemSelectionChanged.connect(self._on_selection_changed)
        self.matrix.cellDoubleClicked.connect(
            lambda *_: self._accept_selected())
        lay.addWidget(self.matrix)
        self.preview = QLabel("")
        self.preview.setObjectName("dim")
        self.preview.setWordWrap(True)
        lay.addWidget(self.preview)
        return box

    def _build_configs_view(self) -> QWidget:
        configs = self.runbook.configs
        columns: List[str] = []
        for record in configs.values():
            for key in record:
                if key not in columns:
                    columns.append(key)
        table = QTableWidget(len(configs), len(columns) or 1)
        table.setHorizontalHeaderLabels(columns or ["(no configs)"])
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        for row, record in enumerate(configs.values()):
            for col, key in enumerate(columns):
                table.setItem(row, col,
                              QTableWidgetItem(_dash(record.get(key))))
        table.resizeColumnsToContents()
        return table

    def _build_named_view(self) -> QWidget:
        named = self.runbook.named_arrays
        table = QTableWidget(len(named), 2)
        table.setHorizontalHeaderLabels(("name", "definition"))
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.horizontalHeader().setStretchLastSection(True)
        for row, (name, definition) in enumerate(named.items()):
            table.setItem(row, 0, QTableWidgetItem(f"@{name}"))
            table.setItem(row, 1, QTableWidgetItem(str(definition)))
        table.resizeColumnsToContents()
        return table

    @staticmethod
    def _section_label(text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            f"color: {theme.ACCENT_LIGHT}; font-weight: bold; margin-top: 4px;")
        return lbl

    # ── selection ──────────────────────────────────────────────────────────
    def _select_first_enabled(self) -> None:
        for row, run in enumerate(self.runbook.runs):
            if run.enable:
                self.matrix.selectRow(row)
                return
        if self.runbook.runs:
            self.matrix.selectRow(0)

    def _current_row(self) -> Optional[int]:
        rows = self.matrix.selectionModel().selectedRows() \
            if self.matrix.selectionModel() else []
        if rows:
            return rows[0].row()
        return None

    def _on_selection_changed(self) -> None:
        row = self._current_row()
        if row is None or not (0 <= row < len(self.runbook.runs)):
            self.preview.setText("")
            return
        run = self.runbook.runs[row]
        try:
            n = expanded_count(self.runbook, run)
        except Exception as exc:                           # noqa: BLE001
            self.preview.setText(f"cannot expand {run.run}: {exc}")
            return
        state = "enabled" if run.enable else "DISABLED"
        self.preview.setText(
            f"{run.run} · config: {run.config_name or '—'} · "
            f"M[{run.mach_cell or '—'}] × β[{run.beta_cell or '—'}] × "
            f"α[{run.alpha_cell or '—'}]  →  {n} points  ({state})")

    # ── accept paths ───────────────────────────────────────────────────────
    def _accept_selected(self) -> None:
        row = self._current_row()
        if row is None or not (0 <= row < len(self.runbook.runs)):
            return
        self.selected_run = self.runbook.runs[row]
        self.select_all_enabled = False
        self.accept()

    def _accept_all_enabled(self) -> None:
        self.selected_run = None
        self.select_all_enabled = True
        self.accept()

    # ── result ─────────────────────────────────────────────────────────────
    def result_points(self) -> Tuple[Optional[RunRow], List[SweepPoint]]:
        """Return ``(run_row, points)`` for the accepted selection.

        For "all enabled runs" ``run_row`` is ``None`` and the points are
        every enabled row's expansion concatenated (each row keeps its own
        ``row_index``).
        """
        if self.select_all_enabled:
            points: List[SweepPoint] = []
            for run in self.runbook.enabled_runs:
                points.extend(build_run_points(self.runbook, run))
            return None, points
        if self.selected_run is not None:
            return (self.selected_run,
                    build_run_points(self.runbook, self.selected_run))
        return None, []
