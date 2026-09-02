"""Custom-mode device picker — choose the active device set by hand.

A checkbox list of every device in the manifest's ``devices`` registry,
each row showing its label and HAL capability tags (from
``hal.capabilities``). The operator ticks any subset; the main window
then builds the DeviceManager from EXACTLY that subset, inferring roles
from capabilities (first Positioner → positioner, first SetpointDevice →
tunnel, Streaming → data, Zeroables usable).

The catalog (id → (label, capability-tags[, unavailable-reason])) is
built by the caller by instantiating each adapter once in sim — the
AUTO-DETECT probe: an adapter whose driver cannot even import/construct
in sim can never be part of a working custom set, so its row renders
disabled with the failure reason instead of a checkbox the operator can
tick. This dialog stays pure Qt and touches no hardware or files itself:
re-detection (``refresh``) and saved-mode deletion (``on_delete_mode``)
are injected callables owned by the main window.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                             QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QPushButton, QScrollArea, QVBoxLayout, QWidget)

from .. import theme

_PILL_CSS = ("border-radius: 8px; padding: 1px 8px; font-weight: bold; "
             "font-size: 8pt;")

#: catalog entry: (label, caps) — legacy — or (label, caps, reason);
#: a non-empty reason marks the device UNAVAILABLE.
CatalogEntry = Tuple[str, Sequence[str]]


def _short(reason: str, limit: int = 48) -> str:
    """First line of *reason*, ellipsised for the row pill."""
    line = str(reason).splitlines()[0] if reason else ""
    return line if len(line) <= limit else line[:limit - 1] + "…"


class DevicePickerDialog(QDialog):
    """Modal checkbox picker. ``catalog`` maps device id → (label, caps)
    or (label, caps, unavailable-reason): rows with a non-empty reason
    are DISABLED (unchecked, uncheckable) with the reason on the row and
    tooltip, so a set that cannot build cannot be picked.

    ``preselected`` seeds the ticked rows (a saved custom set). Read the
    result with :meth:`selected_devices` after ``exec()`` returns
    Accepted, and :meth:`save_mode_name` for the optional "Save as mode"
    name (empty = a one-off set).

    Keyword extras (all optional, main-window injected):

    * ``refresh``      — zero-arg callable returning a fresh catalog;
      adds a "Detect devices" button that re-runs the availability probe
      (current ticks are preserved for rows that stay available).
    * ``saved_modes``  — existing saved-mode names; shows the manage row
      (a combo + explicit Delete button).
    * ``on_delete_mode`` — called with a name when Delete is clicked.
    * ``current_name`` — prefills the save-name field (editing an active
      saved mode overwrites it on OK; blank the field for a one-off).
    """

    def __init__(self, catalog: Dict[str, CatalogEntry],
                 preselected: Optional[Sequence[str]] = None, parent=None, *,
                 refresh: Optional[Callable[[], Dict[str, CatalogEntry]]]
                 = None,
                 saved_modes: Optional[Sequence[str]] = None,
                 on_delete_mode: Optional[Callable[[str], None]] = None,
                 current_name: str = ""):
        super().__init__(parent)
        self.setWindowTitle("Custom mode — pick active devices")
        self.setModal(True)
        self.setMinimumWidth(600)
        # sizeable list → real min/max buttons (maximizable)
        self.setWindowFlags(self.windowFlags()
                            | Qt.WindowType.WindowMinMaxButtonsHint)
        self.setStyleSheet(theme.get_stylesheet())
        self._boxes: Dict[str, QCheckBox] = {}
        self._refresh_catalog = refresh
        self._on_delete_mode = on_delete_mode

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        intro = QLabel(
            "Choose which devices are active in this custom set. Roles are "
            "inferred from capabilities: the first <b>positioner</b> drives "
            "motion, the first <b>setpoint</b> device is the tunnel, every "
            "<b>streaming</b> device is recorded. Pick at least one device. "
            "Greyed rows failed the driver probe and cannot be picked.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.TEXT_DIM};")
        root.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        self._rows_box = QVBoxLayout(host)
        self._rows_box.setContentsMargins(2, 2, 2, 2)
        self._rows_box.setSpacing(6)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        # count (left) + re-detect (right)
        status_row = QHBoxLayout()
        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet(f"color: {theme.TEXT_DIM};")
        status_row.addWidget(self._count_lbl)
        status_row.addStretch(1)
        if callable(refresh):
            self.detect_btn = QPushButton("Detect devices")
            self.detect_btn.setToolTip(
                "Re-probe every driver (import + sim construct) and "
                "refresh the availability of the rows.")
            self.detect_btn.clicked.connect(self._detect)
            status_row.addWidget(self.detect_btn)
        root.addLayout(status_row)

        # save-as-mode name (empty = one-off custom set)
        save_row = QHBoxLayout()
        save_row.addWidget(QLabel("Save as mode:"))
        self._name_edit = QLineEdit(current_name)
        self._name_edit.setPlaceholderText(
            "name to save this set in the mode list (empty = one-off)")
        self._name_edit.setToolTip(
            "OK with a name here saves/overwrites that mode in the mode "
            "combo (★ entries). Leave empty for an unnamed custom set.")
        save_row.addWidget(self._name_edit, 1)
        root.addLayout(save_row)

        # manage (delete) existing saved modes — explicit, no side doors
        if saved_modes:
            manage_row = QHBoxLayout()
            manage_row.addWidget(QLabel("Saved modes:"))
            self._saved_combo = QComboBox()
            self._saved_combo.addItems(list(saved_modes))
            manage_row.addWidget(self._saved_combo, 1)
            self.delete_btn = QPushButton("Delete")
            self.delete_btn.setToolTip(
                "Remove the selected saved mode from the mode list "
                "(the manifest modes are untouched).")
            self.delete_btn.clicked.connect(self._delete_saved)
            manage_row.addWidget(self.delete_btn)
            root.addLayout(manage_row)
        else:
            self._saved_combo = None

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)

        self._populate(catalog, set(preselected or []))

    # ── rows ─────────────────────────────────────────────────────────────
    def _populate(self, catalog: Dict[str, CatalogEntry],
                  checked: set) -> None:
        """(Re)build the device rows; ``checked`` seeds the ticks."""
        while self._rows_box.count():
            item = self._rows_box.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._boxes.clear()
        for dev_id, entry in catalog.items():
            label, caps = entry[0], entry[1]
            reason = str(entry[2]) if len(entry) > 2 and entry[2] else ""
            self._rows_box.addWidget(self._make_row(
                dev_id, label, caps, dev_id in checked, reason))
        self._rows_box.addStretch(1)
        self._refresh_ok()

    def _make_row(self, dev_id: str, label: str, caps: Sequence[str],
                  checked: bool, reason: str = "") -> QWidget:
        frame = QFrame()
        frame.setObjectName("deviceCard")
        frame.setStyleSheet(
            f"QFrame#deviceCard {{ background-color: {theme.BG_LIGHT}; "
            f"border: 1px solid {theme.BORDER}; border-radius: 6px; }}")
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(10, 6, 10, 6)
        box = QCheckBox(label)
        if reason:
            # auto-detected as unavailable: uncheckable, reason shown
            box.setChecked(False)
            box.setEnabled(False)
            box.setStyleSheet(f"color: {theme.TEXT_DISABLED}; "
                              "background: transparent;")
            tip = f"{dev_id} unavailable — {reason}"
            box.setToolTip(tip)
            frame.setToolTip(tip)
            tag = QLabel(f"{dev_id}  ·  unavailable — {_short(reason)}")
            tag.setStyleSheet(
                f"background: {theme.SURFACE}; color: {theme.ERROR}; "
                f"{_PILL_CSS}")
            tag.setToolTip(tip)
        else:
            box.setChecked(checked)
            box.setStyleSheet("font-weight: bold; background: transparent;")
            tag = QLabel(dev_id + "  ·  " + (" · ".join(caps) or "base"))
            tag.setStyleSheet(
                f"background: {theme.SURFACE}; color: {theme.TEXT_DIM}; "
                f"{_PILL_CSS}")
        box.toggled.connect(self._refresh_ok)
        self._boxes[dev_id] = box
        lay.addWidget(box)
        lay.addStretch(1)
        lay.addWidget(tag)
        return frame

    # ── actions ──────────────────────────────────────────────────────────
    def _detect(self) -> None:
        """Re-run the availability probe; keep the current ticks."""
        if not callable(self._refresh_catalog):
            return
        keep = set(self.selected_devices())
        self._populate(self._refresh_catalog(), keep)

    def _delete_saved(self) -> None:
        """Delete the saved mode selected in the manage combo."""
        if self._saved_combo is None or self._saved_combo.count() == 0:
            return
        name = self._saved_combo.currentText()
        if callable(self._on_delete_mode):
            self._on_delete_mode(name)
        self._saved_combo.removeItem(self._saved_combo.currentIndex())
        self.delete_btn.setEnabled(self._saved_combo.count() > 0)
        if self._name_edit.text().strip() == name:
            self._name_edit.clear()        # don't resurrect it on OK

    def _refresh_ok(self) -> None:
        n = len(self.selected_devices())
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(n > 0)
        self._count_lbl.setText(
            "no devices selected — pick at least one" if n == 0
            else f"{n} device{'s' if n != 1 else ''} selected")

    # ── results ──────────────────────────────────────────────────────────
    def selected_devices(self) -> List[str]:
        """Ids ticked, in manifest/catalog order (unavailable rows can
        never be ticked, so they never appear here)."""
        return [dev_id for dev_id, box in self._boxes.items()
                if box.isChecked() and box.isEnabled()]

    def save_mode_name(self) -> str:
        """The 'Save as mode' name, stripped; "" = don't save."""
        return self._name_edit.text().strip()
