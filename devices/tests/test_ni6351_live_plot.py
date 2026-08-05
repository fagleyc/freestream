"""NI USB-6351 live plot UX — display-LPF edge behaviour, per-channel
show/hide toggles (survive rebinds), clear-plot watermark, wheel guard.
Offscreen, no hardware.
"""

import os
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6")

from PyQt6.QtCore import QPoint, QPointF, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtWidgets import (QApplication, QDoubleSpinBox, QVBoxLayout,
                             QWidget)

from ni_usb_6351 import theme
from ni_usb_6351.app.plots import ChannelHistory, lowpass
from ni_usb_6351.config import NiDaqConfig
from ni_usb_6351.datamodel import ScanRingBuffer, fields_for


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([sys.argv[0]])


def _push(ring, names, rate=100.0, seconds=2.0, t0=0.0):
    n = int(seconds * rate)
    t = t0 + np.arange(n) / rate
    block = {"t": t}
    for nm in names:
        block[nm] = np.ones(n)
        block[f"{nm}_V"] = np.ones(n)
    ring.push_block(block)
    return float(t[-1])


def _history(rate=100.0):
    chans = NiDaqConfig().enabled_channels()
    names = [c.name for c in chans]
    ring = ScanRingBuffer(fields_for(names))
    hist = ChannelHistory()
    hist.set_channels(chans, ring)
    hist.note_rate(rate)
    hist.show()
    return hist, ring, names, chans


# ── display LPF: no windowing end effects ────────────────────────────────
def test_lowpass_edge_hold_no_rolloff():
    # constant input must stay constant at BOTH ends of the window — the
    # old convolve(mode="same") zero-padding drooped the plotted trace to
    # ~half over the kernel half-window at each edge
    x = np.ones(500)
    y = lowpass(x, 1000.0, 10.0)          # 100-sample kernel
    assert y.shape == x.shape
    assert np.allclose(y, 1.0)
    # off / degenerate paths unchanged
    assert lowpass(x, 1000.0, 0.0) is x
    assert lowpass(x[:5], 1000.0, 10.0) is not None


# ── per-channel show/hide ────────────────────────────────────────────────
def test_channel_visibility_survives_rebind(qapp):
    hist, ring, names, chans = _history()
    assert hist.channel_visible("Excitation")
    hist.set_channel_visible("Excitation", False)
    assert not hist._curves["Excitation"].isVisible()
    # rebind with the same names (reconnect) keeps the selection
    hist.set_channels(chans, ring)
    assert not hist._curves["Excitation"].isVisible()
    assert hist._curves["N1"].isVisible()
    hist.set_channel_visible("Excitation", True)
    assert hist._curves["Excitation"].isVisible()


def test_hidden_channel_not_redrawn(qapp):
    hist, ring, names, _chans = _history()
    _push(ring, names)
    hist.set_channel_visible("Excitation", False)
    hist.refresh()
    x_n1, _y = hist._curves["N1"].getData()
    assert x_n1 is not None and len(x_n1) > 10
    x_exc, _y = hist._curves["Excitation"].getData()
    assert x_exc is None or len(x_exc) == 0


# ── clear-plot watermark ─────────────────────────────────────────────────
def test_clear_plot_watermark(qapp):
    hist, ring, names, _chans = _history()
    last_t = _push(ring, names)
    hist.refresh()
    x, _y = hist._curves["N1"].getData()
    n_before = len(x)
    assert n_before > 100

    hist.clear_plot()
    hist.refresh()
    x2, _y = hist._curves["N1"].getData()
    assert x2 is None or len(x2) == 0     # nothing newer than the mark

    # samples arriving AFTER the clear are drawn again
    _push(ring, names, seconds=0.5, t0=last_t + 0.01)
    hist.refresh()
    x3, _y = hist._curves["N1"].getData()
    assert 0 < len(x3) < n_before


# ── wheel guard ──────────────────────────────────────────────────────────
def _wheel(widget):
    pos = QPointF(5.0, 5.0)
    ev = QWheelEvent(pos, widget.mapToGlobal(pos.toPoint()).toPointF(),
                     QPoint(0, 0), QPoint(0, 120),
                     Qt.MouseButton.NoButton,
                     Qt.KeyboardModifier.NoModifier,
                     Qt.ScrollPhase.NoScrollPhase, False)
    QApplication.sendEvent(widget, ev)


def test_wheel_guard_blocks_unfocused_spin(qapp):
    w = QWidget()
    lay = QVBoxLayout(w)
    spin = QDoubleSpinBox()
    spin.setRange(0.0, 100.0)
    spin.setValue(50.0)
    lay.addWidget(spin)
    theme.install_wheel_guard(w)
    assert spin.focusPolicy() == Qt.FocusPolicy.StrongFocus
    w.show()
    qapp.processEvents()
    spin.clearFocus()                     # show() focuses the first child
    qapp.processEvents()
    assert not spin.hasFocus()

    _wheel(spin)                          # unfocused → wheel must NOT edit
    assert spin.value() == 50.0

    spin.setFocus()
    qapp.processEvents()
    if spin.hasFocus():                   # focused → normal wheel behaviour
        _wheel(spin)
        assert spin.value() != 50.0


def _run_all():
    app = QApplication.instance() or QApplication([sys.argv[0]])
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn(app) if fn.__code__.co_argcount else fn()
        print(f"  PASS {fn.__name__}")
    print(f"{len(fns)} ni6351 live-plot tests passed.")


if __name__ == "__main__":
    _run_all()
