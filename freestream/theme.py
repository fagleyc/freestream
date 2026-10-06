"""Freestream theme — thin wrapper over :mod:`freestream.themekit`.

The module-level color names (``BG``, ``TEXT_DIM``, ``ACCENT``…) and
``PALETTE`` are LIVE: the theme manager rewrites them whenever the user
switches theme, so code that reads ``theme.TEXT_DIM`` at paint/build time
always gets the active palette. Don't copy them into module-level
constants at import time — read them when you need them.

Themes: USAFA Night (default), USAFA Day, Classic Dark (the original
palette), Classic Light, High Contrast. View ▸ Theme / Appearance… switch
them live; the choice persists between sessions.
"""

from __future__ import annotations

import sys

from . import themekit
from .themekit import (AppearanceDialog, BrandBadge, CommandPalette,  # noqa: F401
                       PALETTES, ThemeToggleButton, app_icon, current,
                       install_view_menu, logo_pixmap, make_splash_pixmap,
                       manager, mix)

# ── palette (initial values = USAFA Night; rewritten live on switch) ──
BG = BG_LIGHT = BG_LIGHTER = SURFACE = ""
TEXT = TEXT_DIM = TEXT_DISABLED = ON_ACCENT = ""
ACCENT = ACCENT_LIGHT = ACCENT_DARK = INDICATOR = ""
SUCCESS = WARNING = ERROR = INFO = ""
BORDER = BORDER_LIGHT = SELECTION = HOVER = ""
HEADER_BG = HEADER_TEXT = HEADER_DIM = ""
PLOT_BG = PLOT_FIG = GRID = AXIS = ""
ROW_ACTIVE = ROW_DONE = ROW_FAILED = ""

# ── data-viz series palette (categorical, CVD-validated per surface) ──
# Channels get colors by enabled order; a color follows its channel in every
# panel. Updated IN PLACE on theme switch (dark/light-stepped hues, same
# order), so hold the list, not its items.
PALETTE: list = []

themekit.manager().register_sink(sys.modules[__name__], series_list=PALETTE)


def series_color(index: int) -> str:
    return PALETTE[index % len(PALETTE)]


def _extra_css(p, scale, density) -> str:
    c = p.tokens
    return f"""
    /* Freestream: command bar sits on the brand header color */
    QToolBar#commandBar {{ background-color: {c['HEADER_BG']};
        border-bottom: 2px solid {c['INDICATOR'] if p.branded else c['BORDER']}; }}
    QToolBar#commandBar QLabel {{ color: {c['HEADER_TEXT']}; }}
    QToolBar#commandBar QLabel#brandTitle {{ color: {c['HEADER_TEXT']}; }}
    QToolBar#commandBar QLabel#brandSub {{ color: {c['HEADER_DIM']}; }}
    QToolBar#commandBar::separator {{ background-color: {mix(c['HEADER_BG'], c['HEADER_TEXT'], 0.25)}; }}
    QToolBar#commandBar QComboBox {{ background-color: {mix(c['HEADER_BG'], c['HEADER_TEXT'], 0.10)};
        border-color: {mix(c['HEADER_BG'], c['HEADER_TEXT'], 0.28)}; color: {c['HEADER_TEXT']}; }}
    QWidget#barSpacer {{ background: transparent; }}
    QLabel#headerStatus {{ color: {c['HEADER_DIM']}; padding: 0 10px; }}
    QLabel#simBadge {{ background: {c['ACCENT_DARK'] if not p.branded else c['ACCENT']};
        color: {c['ON_ACCENT']}; border-radius: 9px; padding: 3px 12px;
        font-weight: bold; letter-spacing: 1px; }}
    QLabel#simBadge[live="true"] {{ background: {c['ERROR']}; }}
    QPushButton#paneHandle {{ background: transparent; border: none;
        color: {c['TEXT_DIM']}; font-size: 8pt; padding: 0; min-height: 0; }}
    QPushButton#paneHandle:hover {{ background: {c['SURFACE']};
        color: {c['ACCENT_LIGHT']}; border-radius: 3px; }}
    QPushButton#paneHandle:checked {{ background: transparent;
        color: {c['TEXT_DIM']}; font-weight: normal; }}
    QPushButton#paneHandle:checked:hover {{ background: {c['SURFACE']};
        color: {c['ACCENT_LIGHT']}; }}
    QToolBar#commandBar QPushButton[flat_header="true"] {{
        background-color: {mix(c['HEADER_BG'], c['HEADER_TEXT'], 0.10)};
        border-color: {mix(c['HEADER_BG'], c['HEADER_TEXT'], 0.28)}; color: {c['HEADER_TEXT']}; }}
    QToolBar#commandBar QPushButton[flat_header="true"]:hover {{
        background-color: {mix(c['HEADER_BG'], c['HEADER_TEXT'], 0.18)}; }}
    QToolBar#commandBar QPushButton[flat_header="true"]:disabled {{
        background-color: {mix(c['HEADER_BG'], c['HEADER_TEXT'], 0.05)};
        color: {mix(c['HEADER_BG'], c['HEADER_TEXT'], 0.40)}; }}
    """


themekit.manager().add_stylesheet_extra(_extra_css)


def get_stylesheet() -> str:
    """The active theme's full application stylesheet."""
    return themekit.manager().stylesheet()


def ensure_applied() -> None:
    """Apply the active theme to the running QApplication once (window
    constructors call this; entry points call ``manager().init``)."""
    themekit.manager().ensure_applied()


def apply_pyqtgraph_theme() -> None:
    """Set pyqtgraph global options to the active palette.

    Call before plot widgets are created (the manager re-calls it on every
    theme switch and restyles existing plots)."""
    import pyqtgraph as pg
    pg.setConfigOption("background", PLOT_BG)
    pg.setConfigOption("foreground", TEXT_DIM)
    pg.setConfigOption("antialias", True)


def install_wheel_guard(root) -> None:
    """Stop the mouse wheel from editing spin/combo boxes the pointer
    merely passes over while scrolling: they take wheel input only when
    focused (clicked/tabbed into). Arrow-button and keyboard behaviour
    are untouched. Call after a panel's widgets are built; safe to call
    again after dynamic rebuilds."""
    from PyQt6.QtCore import QEvent, QObject, Qt
    from PyQt6.QtWidgets import QAbstractSpinBox, QComboBox

    class _WheelGuard(QObject):
        def eventFilter(self, obj, ev):                # noqa: N802
            return (ev.type() == QEvent.Type.Wheel
                    and not obj.hasFocus())

    guard = getattr(root, "_wheel_guard", None)
    if guard is None:
        guard = _WheelGuard(root)
        root._wheel_guard = guard
    for w in root.findChildren((QAbstractSpinBox, QComboBox)):
        w.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        w.installEventFilter(guard)
