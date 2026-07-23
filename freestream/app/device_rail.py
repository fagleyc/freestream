"""Left-dock device rail — one card per registered device.

Cards are generated PURELY from ``manager.devices`` + the HAL capability
protocols: a new device in the manifest appears here with zero UI code
(spec acceptance criterion). A 500 ms QTimer polls ``manager.all_status()``.
"""

from __future__ import annotations

from typing import Dict, Optional

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QCursor
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QScrollArea,
                             QVBoxLayout, QWidget)

from .. import theme
from ..hal import FAULT, OK, DeviceStatus, Streaming, capabilities
from ..manager import DeviceManager

_PILL_CSS = ("border-radius: 8px; padding: 1px 8px; font-weight: bold; "
             "font-size: 8pt;")
_STATE_STYLE = {
    OK: f"background: {theme.SUCCESS}; color: white; {_PILL_CSS}",
    FAULT: f"background: {theme.ERROR}; color: white; {_PILL_CSS}",
    "OFFLINE": (f"background: {theme.SURFACE}; color: {theme.TEXT_DIM}; "
                f"{_PILL_CSS}"),
}


class DeviceCard(QFrame):
    """Registry-driven card: label, capability tags, status pill, sim
    badge, last-sample age and (for Streaming devices) a mini readout.

    Clicking the card (when the wrapped adapter offers a settings dialog)
    emits :attr:`clicked` with the device id — the main window then opens
    that device's OWN driver-configuration dialog."""

    clicked = pyqtSignal(str)

    def __init__(self, dev, parent=None):
        super().__init__(parent)
        self.dev = dev
        self.setObjectName("deviceCard")
        self._configurable = callable(getattr(dev, "config_dict", None))
        self.setStyleSheet(
            f"QFrame#deviceCard {{ background-color: {theme.BG_LIGHT};"
            f" border: 1px solid {theme.BORDER}; border-radius: 6px; }}"
            f"QFrame#deviceCard:hover {{ border: 1px solid {theme.ACCENT}; }}")
        if self._configurable:
            self.setCursor(QCursor(Qt.CursorShape.PointingHandCursor))
            self.setToolTip(f"Click to configure {getattr(dev,'id',dev)}")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(3)

        top = QHBoxLayout()
        name = QLabel(getattr(dev, "label", dev.id))
        name.setStyleSheet("font-weight: bold; background: transparent;")
        # compressible: a long label WRAPS instead of forcing the card
        # wider than the dock viewport (which clipped the status pill
        # and the readout line at the card's right edge)
        name.setWordWrap(True)
        name.setMinimumWidth(1)
        top.addWidget(name, 1)
        if self._configurable:
            gear = QLabel("⚙")
            gear.setToolTip("Click card to configure this device")
            gear.setStyleSheet(f"color: {theme.TEXT_DIM}; "
                               "background: transparent;")
            top.addWidget(gear)
        self.sim_badge = QLabel("SIM")
        self.sim_badge.setStyleSheet(
            f"background: {theme.ACCENT_DARK}; color: white; {_PILL_CSS}")
        self.sim_badge.hide()
        top.addWidget(self.sim_badge)
        self.pill = QLabel("OFFLINE")
        self.pill.setStyleSheet(_STATE_STYLE["OFFLINE"])
        top.addWidget(self.pill)
        lay.addLayout(top)

        caps = capabilities(dev)
        tags = QLabel(f"{dev.id}  ·  " + (" · ".join(caps) or "base"))
        tags.setObjectName("dim")
        tags.setStyleSheet(f"color: {theme.TEXT_DIM}; font-size: 8pt; "
                           "background: transparent;")
        lay.addWidget(tags)

        self.age_lbl = QLabel("")
        self.age_lbl.setStyleSheet(f"color: {theme.TEXT_DIM}; "
                                   "font-size: 8pt; background: transparent;")
        lay.addWidget(self.age_lbl)

        self.readout: Optional[QLabel] = None
        if isinstance(dev, Streaming):
            self.readout = QLabel("—")
            self.readout.setStyleSheet(
                f"color: {theme.SUCCESS}; font-family: Consolas, monospace;"
                " font-size: 8pt; background: transparent;")
            self.readout.setWordWrap(True)
            lay.addWidget(self.readout)

        self.fault_lbl = QLabel("")
        self.fault_lbl.setStyleSheet(f"color: {theme.ERROR}; font-size: 8pt;"
                                     " background: transparent;")
        self.fault_lbl.setWordWrap(True)
        self.fault_lbl.hide()
        lay.addWidget(self.fault_lbl)

    def refresh(self, st: DeviceStatus) -> None:
        self.pill.setText(st.state)
        self.pill.setStyleSheet(_STATE_STYLE.get(st.state,
                                                 _STATE_STYLE["OFFLINE"]))
        self.sim_badge.setVisible(bool(st.sim))
        if st.last_sample_age_s is None:
            self.age_lbl.setText("last sample: —")
        else:
            self.age_lbl.setText(f"last sample: {st.last_sample_age_s:.1f} s")
        if st.state == FAULT and st.message:
            self.fault_lbl.setText(st.message)
            self.fault_lbl.show()
        else:
            self.fault_lbl.hide()
        if self.readout is not None:
            if st.state == OK:
                try:
                    vals = self.dev.latest()
                    self.readout.setText("  ".join(
                        f"{k}={v:.3g}" for k, v in list(vals.items())[:4]))
                except Exception:                      # noqa: BLE001
                    self.readout.setText("—")
            else:
                self.readout.setText("—")

    connectRequested = pyqtSignal(str)
    disconnectRequested = pyqtSignal(str)

    def mousePressEvent(self, event) -> None:          # noqa: N802
        if (self._configurable
                and event.button() == Qt.MouseButton.LeftButton):
            self.clicked.emit(getattr(self.dev, "id", ""))
        super().mousePressEvent(event)

    def contextMenuEvent(self, event) -> None:         # noqa: N802
        from PyQt6.QtWidgets import QMenu
        dev_id = getattr(self.dev, "id", "")
        menu = QMenu(self)
        if self._configurable:
            act = menu.addAction("Configure…")
            act.triggered.connect(lambda: self.clicked.emit(dev_id))
            menu.addSeparator()
        if getattr(self.dev, "connected", False):
            act = menu.addAction("Disconnect device")
            act.triggered.connect(
                lambda: self.disconnectRequested.emit(dev_id))
        else:
            act = menu.addAction("Connect device")
            act.triggered.connect(
                lambda: self.connectRequested.emit(dev_id))
        menu.exec(event.globalPos())


class DeviceRail(QWidget):
    """Scrollable stack of DeviceCards; rebuilt on mode switch.

    Emits :attr:`deviceClicked` when a card is clicked (opens the full
    device configuration GUI) and connect/disconnect requests from each
    card's context menu (single-device bring-up)."""

    deviceClicked = pyqtSignal(str)
    deviceConnectRequested = pyqtSignal(str)
    deviceDisconnectRequested = pyqtSignal(str)

    def __init__(self, manager: DeviceManager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self._cards: Dict[str, DeviceCard] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._host = QWidget()
        self._vbox = QVBoxLayout(self._host)
        self._vbox.setContentsMargins(6, 6, 6, 6)
        self._vbox.setSpacing(6)
        self._vbox.addStretch(1)
        scroll.setWidget(self._host)
        outer.addWidget(scroll)

        self._rebuild()

        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self.poll)
        self._timer.start()

    # ── registry sync ────────────────────────────────────────────────────
    def set_manager(self, manager: DeviceManager) -> None:
        self.manager = manager
        self._rebuild()

    def _rebuild(self) -> None:
        for card in self._cards.values():
            self._vbox.removeWidget(card)
            card.deleteLater()
        self._cards.clear()
        for dev_id, dev in self.manager.devices.items():
            card = DeviceCard(dev)
            card.clicked.connect(self.deviceClicked)
            card.connectRequested.connect(self.deviceConnectRequested)
            card.disconnectRequested.connect(
                self.deviceDisconnectRequested)
            self._cards[dev_id] = card
            self._vbox.insertWidget(self._vbox.count() - 1, card)
        self.poll()

    def poll(self) -> None:
        try:
            statuses = self.manager.all_status()
        except Exception:                              # noqa: BLE001
            return
        for dev_id, card in self._cards.items():
            st = statuses.get(dev_id)
            if st is not None:
                card.refresh(st)

    def shutdown(self) -> None:
        self._timer.stop()
