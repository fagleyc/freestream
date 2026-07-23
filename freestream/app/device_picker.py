"""Custom-mode device picker — choose the active device set by hand.

A checkbox list of every device in the manifest's ``devices`` registry,
each row showing its label and HAL capability tags (from
``hal.capabilities``). The operator ticks any subset; the main window
then builds the DeviceManager from EXACTLY that subset, inferring roles
from capabilities (first Positioner → positioner, first SetpointDevice →
tunnel, Streaming → data, Zeroables usable).

The catalog (id → (label, capability-tags)) is built by the caller by
instantiating each adapter once in sim, so this dialog stays pure Qt and
touches no hardware.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QFrame,
                             QHBoxLayout, QLabel, QScrollArea, QVBoxLayout,
                             QWidget)

from .. import theme

_PILL_CSS = ("border-radius: 8px; padding: 1px 8px; font-weight: bold; "
             "font-size: 8pt;")


class DevicePickerDialog(QDialog):
    """Modal checkbox picker. ``catalog`` maps device id → (label, caps).

    ``preselected`` seeds the ticked rows (a saved custom set). Read the
    result with :meth:`selected_devices` after ``exec()`` returns Accepted.
    """

    def __init__(self, catalog: Dict[str, Tuple[str, Sequence[str]]],
                 preselected: Optional[Sequence[str]] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Custom mode — pick active devices")
        self.setModal(True)
        self.setMinimumWidth(600)
        # sizeable list → real min/max buttons (maximizable)
        self.setWindowFlags(self.windowFlags()
                            | Qt.WindowType.WindowMinMaxButtonsHint)
        self.setStyleSheet(theme.get_stylesheet())
        self._boxes: Dict[str, QCheckBox] = {}
        pre = set(preselected or [])

        root = QVBoxLayout(self)
        root.setContentsMargins(14, 14, 14, 14)
        root.setSpacing(10)

        intro = QLabel(
            "Choose which devices are active in this custom set. Roles are "
            "inferred from capabilities: the first <b>positioner</b> drives "
            "motion, the first <b>setpoint</b> device is the tunnel, every "
            "<b>streaming</b> device is recorded. Pick at least one device.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {theme.TEXT_DIM};")
        root.addWidget(intro)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        host = QWidget()
        vbox = QVBoxLayout(host)
        vbox.setContentsMargins(2, 2, 2, 2)
        vbox.setSpacing(6)
        for dev_id, (label, caps) in catalog.items():
            vbox.addWidget(self._make_row(dev_id, label, caps,
                                          dev_id in pre))
        vbox.addStretch(1)
        scroll.setWidget(host)
        root.addWidget(scroll, 1)

        self._count_lbl = QLabel("")
        self._count_lbl.setStyleSheet(f"color: {theme.TEXT_DIM};")
        root.addWidget(self._count_lbl)

        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel)
        self._buttons.accepted.connect(self.accept)
        self._buttons.rejected.connect(self.reject)
        root.addWidget(self._buttons)
        self._refresh_ok()

    def _make_row(self, dev_id: str, label: str,
                  caps: Sequence[str], checked: bool) -> QWidget:
        frame = QFrame()
        frame.setObjectName("deviceCard")
        frame.setStyleSheet(
            f"QFrame#deviceCard {{ background-color: {theme.BG_LIGHT}; "
            f"border: 1px solid {theme.BORDER}; border-radius: 6px; }}")
        lay = QHBoxLayout(frame)
        lay.setContentsMargins(10, 6, 10, 6)
        box = QCheckBox(label)
        box.setChecked(checked)
        box.setStyleSheet("font-weight: bold; background: transparent;")
        box.toggled.connect(self._refresh_ok)
        self._boxes[dev_id] = box
        lay.addWidget(box)
        lay.addStretch(1)
        tag = QLabel(dev_id + "  ·  " + (" · ".join(caps) or "base"))
        tag.setStyleSheet(
            f"background: {theme.SURFACE}; color: {theme.TEXT_DIM}; "
            f"{_PILL_CSS}")
        lay.addWidget(tag)
        return frame

    def _refresh_ok(self) -> None:
        n = len(self.selected_devices())
        ok = self._buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setEnabled(n > 0)
        self._count_lbl.setText(
            "no devices selected — pick at least one" if n == 0
            else f"{n} device{'s' if n != 1 else ''} selected")

    def selected_devices(self) -> List[str]:
        """Ids ticked, in manifest/catalog order."""
        return [dev_id for dev_id, box in self._boxes.items()
                if box.isChecked()]
