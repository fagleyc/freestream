"""themekit — live-switchable themes, view controls and USAFA branding.

Shared by Freestream and Streamlined. The two copies
(``freestream/freestream/themekit.py`` and
``Streamlined/utils/gui/utils/themekit.py``) are IDENTICAL; edit one, copy
it over the other. App-specific stylesheet extras stay in each app's own
``theme.py`` / ``themes.py`` wrapper.

What it provides
----------------
* ``PALETTES`` — USAFA Night / USAFA Day (official usafa.edu brand colors),
  Classic Dark (the original palette, value-for-value), Classic Light and
  High Contrast. Every palette carries the same token names (``BG``,
  ``TEXT_DIM``, ``ACCENT``…) plus a categorical data-viz series stepped
  for its own surface.
* ``ThemeManager`` (``manager()``) — applies a palette to the running app
  WITHOUT a restart: rebuilds the app stylesheet, rewrites token values into
  every registered sink (module globals / class attributes, so existing
  ``theme.TEXT_DIM`` reads stay valid), remaps colors already baked into
  per-widget stylesheets, label rich text and pyqtgraph items, updates the
  matplotlib rcParams, then emits ``changed`` (embedded matplotlib canvases
  restyle themselves on that signal — a generic artist sweep proved unsafe
  under the Windows Qt/matplotlib GC hazard). UI scale and density ride along. Persistence is opt-in via
  ``init(app, settings)`` so tests never read or write the user's registry.
* View tooling — ``install_view_menu`` (Theme ▸, Appearance…, zoom, full
  screen, command palette), ``AppearanceDialog`` (theme cards with live
  preview), ``CommandPalette`` (Ctrl+K fuzzy action search),
  ``ThemeToggleButton`` (status-bar light/dark flip).
* Branding — ``BrandBadge`` (Aeronautics roundel + title for toolbars),
  ``app_icon()``, ``make_splash_pixmap()``, ``logo_pixmap()``. USAFA marks
  are never recolored (brand rule); on dark surfaces they sit on a white
  plate instead.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from PyQt6.QtCore import (QByteArray, QEvent, QObject, QPoint, QRect, QRectF,
                          QSettings, QSize, Qt, QTimer, pyqtSignal)
from PyQt6.QtGui import (QAction, QActionGroup, QBrush, QColor, QFont,
                         QIcon, QIconEngine, QKeySequence, QLinearGradient,
                         QPainter, QPainterPath, QPen, QPixmap, QShortcut)
from PyQt6.QtWidgets import (QAbstractButton, QApplication, QButtonGroup,
                             QComboBox, QDialog, QDialogButtonBox, QFrame,
                             QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                             QListWidget, QListWidgetItem, QMainWindow, QMenu,
                             QSizePolicy, QSlider, QToolButton, QVBoxLayout,
                             QWidget)

ASSETS = Path(__file__).resolve().parent / "assets" / "usafa"
if not ASSETS.is_dir():                      # Streamlined layout: utils/../assets
    ASSETS = Path(__file__).resolve().parents[1] / "assets" / "usafa"

ORG_LINE = "U.S. Air Force Academy · Department of Aeronautics"

# ════════════════════════════════════════════════════════════════════════
#  Palettes
# ════════════════════════════════════════════════════════════════════════

#: Official USAFA brand colors (usafa.edu/brand/brand-colors).
USAFA = {
    "academy_blue": "#003594",   # PMS 661 C
    "class_royal": "#002554",    # PMS 655 C
    "academy_grey": "#b2b4b2",   # PMS 421 C
    "class_red": "#a6192e",      # PMS 187 C
    "class_yellow": "#ffc72c",   # PMS 123 C
    "grotto_blue": "#00bed6",
}

#: Token names every palette defines. Order matters only for documentation.
TOKENS = (
    "BG", "BG_LIGHT", "BG_LIGHTER", "SURFACE", "HOVER", "SELECTION",
    "BORDER", "BORDER_LIGHT",
    "TEXT", "TEXT_DIM", "TEXT_DISABLED", "ON_ACCENT",
    "ACCENT", "ACCENT_LIGHT", "ACCENT_DARK", "INDICATOR",
    "SUCCESS", "WARNING", "ERROR", "INFO",
    "HEADER_BG", "HEADER_TEXT", "HEADER_DIM",
    "GRID", "AXIS",
)
#: Tokens that are the same color as another token in EVERY palette. They
#: are resolved, never stored, so the color remap stays unambiguous.
ALIASES = {"PLOT_BG": "BG_LIGHT", "PLOT_FIG": "BG"}
#: Tokens mixed from others when the palette is built (row tints sit
#: behind text in the sweep table, so they are a light wash of the status).
DERIVED = {
    "ROW_ACTIVE": ("WARNING", "BG_LIGHT", 0.80),
    "ROW_DONE": ("SUCCESS", "BG_LIGHT", 0.82),
    "ROW_FAILED": ("ERROR", "BG_LIGHT", 0.80),
}

# categorical series, CVD-validated (dataviz reference palette): the dark
# column for dark surfaces, the light column for light surfaces. Same hue
# order in both, so a channel keeps its hue when the theme flips.
# Reference slot order (blue, orange, aqua, yellow, magenta, green, violet,
# red) — validated with dataviz/validate_palette.js on every theme surface:
# adjacent CVD dE >= 8.4, normal-vision dE >= 19.3; on the light surfaces
# aqua/yellow/magenta sit under 3:1, so plots keep legends/labels.
SERIES_DARK = ("#3987e5", "#d95926", "#199e70", "#c98500",
               "#d55181", "#008300", "#9085e9", "#e66767")
SERIES_LIGHT = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                "#e87ba4", "#008300", "#4a3aa7", "#e34948")


def mix(a: str, b: str, t: float) -> str:
    """Blend ``a`` toward ``b`` by ``t`` (0 = a, 1 = b) → ``#rrggbb``."""
    ca, cb = QColor(a), QColor(b)
    r = round(ca.red() + (cb.red() - ca.red()) * t)
    g = round(ca.green() + (cb.green() - ca.green()) * t)
    bl = round(ca.blue() + (cb.blue() - ca.blue()) * t)
    return f"#{r:02x}{g:02x}{bl:02x}"


@dataclass
class Palette:
    key: str
    name: str
    dark: bool
    description: str
    colors: Dict[str, str]
    series: Tuple[str, ...]
    pair: str                                 # light/dark counterpart key
    font_ui: str = "Segoe UI"
    font_heading: str = "Segoe UI"
    font_mono: str = "Consolas"
    branded: bool = False                     # show USAFA brand bar styling
    tokens: Dict[str, str] = field(init=False)

    def __post_init__(self):
        missing = [t for t in TOKENS if t not in self.colors]
        if missing:
            raise ValueError(f"palette {self.key!r} missing {missing}")
        tok = {k: v.lower() for k, v in self.colors.items()}
        for name, (a, b, t) in DERIVED.items():
            tok[name] = mix(tok[a], tok[b], t)
        for alias, src in ALIASES.items():
            tok[alias] = tok[src]
        self.tokens = tok

    def __getitem__(self, token: str) -> str:
        return self.tokens[token]

    def get(self, token: str, default: str = "") -> str:
        return self.tokens.get(token, default)

    def series_color(self, i: int) -> str:
        return self.series[i % len(self.series)]

    def stored_tokens(self) -> Dict[str, str]:
        """Tokens that own a color (aliases excluded) — the remap keys."""
        return {k: v for k, v in self.tokens.items() if k not in ALIASES}


def _usafa_night() -> Palette:
    return Palette(
        "usafa_night", "USAFA Night", True,
        "Class Royal navy surfaces, Academy Blue controls, Class Yellow "
        "focus. The default for the control room.",
        {
            "BG": "#0a1424", "BG_LIGHT": "#0f1c31", "BG_LIGHTER": "#15253e",
            "SURFACE": "#1b2d4b", "HOVER": "#24395d", "SELECTION": "#1f4a8a",
            "BORDER": "#283c5c", "BORDER_LIGHT": "#3a5078",
            "TEXT": "#e7ecf3", "TEXT_DIM": "#b2b4b2",
            "TEXT_DISABLED": "#5b6a82", "ON_ACCENT": "#fcfdff",
            "ACCENT": "#2e6bd6", "ACCENT_LIGHT": "#79a6f0",
            "ACCENT_DARK": "#003594", "INDICATOR": "#ffc72c",
            "SUCCESS": "#2ea043", "WARNING": "#f0a020", "ERROR": "#e5484d",
            "INFO": "#00bed6",
            "HEADER_BG": "#002554", "HEADER_TEXT": "#f4f6fa",
            "HEADER_DIM": "#aab7cc",
            "GRID": "#1a2a44", "AXIS": "#45597c",
        },
        SERIES_DARK, pair="usafa_day", font_heading="Trebuchet MS",
        branded=True)


def _usafa_day() -> Palette:
    return Palette(
        "usafa_day", "USAFA Day", False,
        "White and Academy Grey surfaces with Academy Blue controls and a "
        "Class Royal header. Best in bright rooms and for screenshots.",
        {
            "BG": "#eef1f5", "BG_LIGHT": "#fafbfd", "BG_LIGHTER": "#e3e8ef",
            "SURFACE": "#fdfeff", "HOVER": "#dce4ef", "SELECTION": "#cddcf3",
            "BORDER": "#c5cdd8", "BORDER_LIGHT": "#a9b3c1",
            "TEXT": "#0e1a2c", "TEXT_DIM": "#4c5868",
            "TEXT_DISABLED": "#9aa3af", "ON_ACCENT": "#fefefe",
            "ACCENT": "#003594", "ACCENT_LIGHT": "#1d55b8",
            "ACCENT_DARK": "#002554", "INDICATOR": "#a67c00",
            "SUCCESS": "#1a7f37", "WARNING": "#b35900", "ERROR": "#a6192e",
            "INFO": "#007a91",
            "HEADER_BG": "#002553", "HEADER_TEXT": "#f5f7fb",
            "HEADER_DIM": "#b2c0d6",
            "GRID": "#e1e6ed", "AXIS": "#7f8a9a",
        },
        SERIES_LIGHT, pair="usafa_night", font_heading="Trebuchet MS",
        branded=True)


def _classic_dark() -> Palette:
    # value-for-value the original Streamlined/Freestream dark palette
    return Palette(
        "classic_dark", "Classic Dark", True,
        "The original VS-Code-style dark theme, unchanged.",
        {
            "BG": "#1e1e1e", "BG_LIGHT": "#252526", "BG_LIGHTER": "#2d2d30",
            "SURFACE": "#333333", "HOVER": "#3a3a3c", "SELECTION": "#264f78",
            "BORDER": "#3f3f46", "BORDER_LIGHT": "#4f4f56",
            "TEXT": "#e0e0e0", "TEXT_DIM": "#a0a0a0",
            "TEXT_DISABLED": "#606060", "ON_ACCENT": "#fdfdfd",
            "ACCENT": "#0078d4", "ACCENT_LIGHT": "#3399ff",
            "ACCENT_DARK": "#005a9e", "INDICATOR": "#1a8cff",
            "SUCCESS": "#4caf50", "WARNING": "#ff9800", "ERROR": "#f44336",
            "INFO": "#2196f3",
            "HEADER_BG": "#181818", "HEADER_TEXT": "#ececec",
            "HEADER_DIM": "#9b9b9b",
            "GRID": "#2c2c2a", "AXIS": "#4a4a4f",
        },
        SERIES_DARK, pair="classic_light")


def _classic_light() -> Palette:
    return Palette(
        "classic_light", "Classic Light", False,
        "Neutral light grey with the classic blue accent.",
        {
            "BG": "#f3f3f3", "BG_LIGHT": "#fbfbfb", "BG_LIGHTER": "#e8e8e8",
            "SURFACE": "#fcfcfd", "HOVER": "#e1e1e3", "SELECTION": "#cce4f7",
            "BORDER": "#d0d0d4", "BORDER_LIGHT": "#b5b5ba",
            "TEXT": "#1b1b1b", "TEXT_DIM": "#5a5a5e",
            "TEXT_DISABLED": "#a3a3a6", "ON_ACCENT": "#fefeff",
            "ACCENT": "#0067c0", "ACCENT_LIGHT": "#005fb8",
            "ACCENT_DARK": "#004c8f", "INDICATOR": "#0a74d0",
            "SUCCESS": "#107c10", "WARNING": "#b04f00", "ERROR": "#c42b1c",
            "INFO": "#0063b1",
            "HEADER_BG": "#e6e6e6", "HEADER_TEXT": "#1a1a1a",
            "HEADER_DIM": "#5f5f5f",
            "GRID": "#e4e4e4", "AXIS": "#8c8c90",
        },
        SERIES_LIGHT, pair="classic_dark")


def _high_contrast() -> Palette:
    return Palette(
        "high_contrast", "High Contrast", True,
        "Near-black surfaces, bright text, heavy borders and yellow focus — "
        "for projectors, glare and low vision.",
        {
            "BG": "#050505", "BG_LIGHT": "#0b0b0b", "BG_LIGHTER": "#151515",
            "SURFACE": "#121212", "HOVER": "#2a2a2a", "SELECTION": "#00458f",
            "BORDER": "#9c9c9c", "BORDER_LIGHT": "#d6d6d6",
            "TEXT": "#fafafa", "TEXT_DIM": "#dedede",
            "TEXT_DISABLED": "#8a8a8a", "ON_ACCENT": "#fffffe",
            "ACCENT": "#1f5fe0", "ACCENT_LIGHT": "#7fb2ff",
            "ACCENT_DARK": "#0b3fa8", "INDICATOR": "#ffe600",
            "SUCCESS": "#22a23a", "WARNING": "#ffb000", "ERROR": "#e3241e",
            "INFO": "#33e1ff",
            "HEADER_BG": "#000001", "HEADER_TEXT": "#fffffd",
            "HEADER_DIM": "#cfcfcf",
            "GRID": "#262626", "AXIS": "#b0b0b0",
        },
        SERIES_DARK, pair="usafa_day")


PALETTES: Dict[str, Palette] = {p.key: p for p in (
    _usafa_night(), _usafa_day(), _classic_dark(), _classic_light(),
    _high_contrast())}
DEFAULT_THEME = "usafa_night"
CLASSIC = PALETTES["classic_dark"]           # what un-synced legacy code bakes

DENSITIES = {"compact": 0.75, "comfortable": 1.0, "spacious": 1.3}
SCALE_RANGE = (0.8, 1.6)


# ════════════════════════════════════════════════════════════════════════
#  Color remapping (live switch of colors already baked into widgets)
# ════════════════════════════════════════════════════════════════════════

_HEX = re.compile(r"#[0-9a-fA-F]{6}\b")


def color_map(old: Palette, new: Palette,
              extra_sources: Sequence[Palette] = ()) -> Dict[str, str]:
    """``{old_hex: new_hex}`` for every stored token and series slot.

    ``extra_sources`` add more FROM palettes (e.g. Classic, which legacy
    device panels bake before they are synced); the first claim on a hex
    wins, so the actual previous palette always takes precedence."""
    out: Dict[str, str] = {}
    for src in (old, *extra_sources):
        for k, v in src.stored_tokens().items():
            out.setdefault(v, new.tokens[k])
        for i, v in enumerate(src.series):
            out.setdefault(v, new.series[i % len(new.series)])
    return {k: v for k, v in out.items() if k != v}


def remap_css(css: str, mapping: Dict[str, str]) -> str:
    if not css or not mapping:
        return css
    return _HEX.sub(lambda m: mapping.get(m.group(0).lower(), m.group(0)), css)


def remap_widget(w: QWidget, mapping: Dict[str, str]) -> None:
    """Remap a widget's own stylesheet and, for labels, inline rich-text
    colors (``<span style='color:#…'>``) — both bake hex values."""
    css = w.styleSheet()
    if css:
        nc = remap_css(css, mapping)
        if nc != css:
            w.setStyleSheet(nc)
    if isinstance(w, QLabel):
        txt = w.text()
        if "#" in txt and "<" in txt:
            nt = remap_css(txt, mapping)
            if nt != txt:
                w.setText(nt)


def _remap_qcolor(c: QColor, mapping: Dict[str, str]) -> Optional[QColor]:
    new = mapping.get(c.name().lower())
    if new is None:
        return None
    out = QColor(new)
    out.setAlpha(c.alpha())
    return out


# ════════════════════════════════════════════════════════════════════════
#  Stylesheet
# ════════════════════════════════════════════════════════════════════════

def _icon_cache_dir(p: Palette) -> Path:
    from PyQt6.QtCore import QStandardPaths
    base = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.TempLocation)
    d = Path(base) / "usafa-themekit" / p.key
    d.mkdir(parents=True, exist_ok=True)
    return d


def _write_glyphs(p: Palette) -> Dict[str, str]:
    """Theme-colored SVG glyphs for check marks and arrows (Qt stylesheets
    can't take data: URIs, so they live in a per-theme temp folder)."""
    d = _icon_cache_dir(p)
    glyphs = {
        "check": (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                  f'<path d="M3.5 8.5l3 3 6-7" fill="none" stroke="{p["ON_ACCENT"]}"'
                  f' stroke-width="2.2" stroke-linecap="round" '
                  f'stroke-linejoin="round"/></svg>'),
        "down": (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                 f'<path d="M4 6l4 4 4-4" fill="none" stroke="{p["TEXT_DIM"]}"'
                 f' stroke-width="1.8" stroke-linecap="round" '
                 f'stroke-linejoin="round"/></svg>'),
        "up": (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
               f'<path d="M4 10l4-4 4 4" fill="none" stroke="{p["TEXT_DIM"]}"'
               f' stroke-width="1.8" stroke-linecap="round" '
               f'stroke-linejoin="round"/></svg>'),
        "dot": (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">'
                f'<circle cx="8" cy="8" r="3.4" fill="{p["ON_ACCENT"]}"/></svg>'),
    }
    out = {}
    for name, svg in glyphs.items():
        f = d / f"{name}.svg"
        try:
            if not f.exists() or f.read_text() != svg:
                f.write_text(svg)
            out[name] = f.as_posix()
        except OSError:
            out[name] = ""
    return out


def build_stylesheet(p: Palette, scale: float = 1.0,
                     density: float = 1.0) -> str:
    """The full application stylesheet for palette ``p``."""
    c = p.tokens
    fs = round(10 * scale, 1)                 # base font pt
    fs_small = round(8.5 * scale, 1)
    pad_v = max(2, round(5 * density))
    pad_h = max(4, round(10 * density))
    btn_v = max(3, round(6 * density))
    btn_h = max(8, round(14 * density))
    row_h = max(18, round(22 * density * scale))
    g = _write_glyphs(p)
    check = f"image: url({g['check']});" if g.get("check") else ""
    down = f"image: url({g['down']});" if g.get("down") else ""
    up = f"image: url({g['up']});" if g.get("up") else ""
    dot = f"image: url({g['dot']});" if g.get("dot") else ""
    head = f'"{p.font_heading}"'
    return f"""
    QMainWindow, QWidget#root {{ background-color: {c['BG']}; }}
    QWidget {{ background-color: {c['BG']}; color: {c['TEXT']};
               font-family: "{p.font_ui}", "Segoe UI", sans-serif;
               font-size: {fs}pt; }}
    QDialog {{ background-color: {c['BG']}; }}

    QGroupBox {{ background-color: {c['BG_LIGHT']}; border: 1px solid {c['BORDER']};
                 border-radius: 8px; margin-top: 14px; padding-top: 12px;
                 font-weight: 600; }}
    QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left;
                        left: 12px; padding: 0 6px; color: {c['ACCENT_LIGHT']};
                        font-family: {head}; letter-spacing: 0.3px; }}

    QLabel {{ background: transparent; color: {c['TEXT']}; }}
    QLabel#dim, QLabel[role="dim"] {{ color: {c['TEXT_DIM']}; }}
    QLabel#mono {{ font-family: "{p.font_mono}", monospace; color: {c['SUCCESS']}; }}
    QLabel#value {{ font-family: "{p.font_mono}", monospace;
                    font-size: {round(17 * scale)}pt; color: {c['ACCENT_LIGHT']}; }}
    QLabel#unit {{ color: {c['TEXT_DIM']}; }}
    QLabel[role="heading"] {{ font-family: {head}; font-size: {round(13 * scale)}pt;
                              font-weight: 600; }}

    QPushButton {{ background-color: {c['SURFACE']}; border: 1px solid {c['BORDER']};
                   border-radius: 6px; padding: {btn_v}px {btn_h}px; color: {c['TEXT']};
                   min-height: {row_h}px; }}
    QPushButton:hover {{ background-color: {c['HOVER']}; border-color: {c['ACCENT']}; }}
    QPushButton:pressed {{ background-color: {c['ACCENT_DARK']}; color: {c['ON_ACCENT']}; }}
    QPushButton:focus {{ border-color: {c['INDICATOR']}; }}
    QPushButton:disabled {{ background-color: {c['BG_LIGHTER']}; color: {c['TEXT_DISABLED']};
                            border-color: {c['BORDER']}; }}
    QPushButton:checked {{ background-color: {c['ACCENT']}; border-color: {c['ACCENT']};
                           color: {c['ON_ACCENT']}; font-weight: bold; }}
    QPushButton#primary, QPushButton[primary="true"] {{
        background-color: {c['ACCENT']}; border: 1px solid {c['ACCENT']};
        color: {c['ON_ACCENT']}; font-weight: bold; }}
    QPushButton#primary:hover, QPushButton[primary="true"]:hover {{
        background-color: {c['ACCENT_LIGHT']}; border-color: {c['ACCENT_LIGHT']}; }}
    QPushButton#success {{ background-color: {c['SUCCESS']}; border: 1px solid {c['SUCCESS']};
                           color: {c['ON_ACCENT']}; font-weight: bold; }}
    QPushButton#danger {{ background-color: {c['ERROR']}; border: 1px solid {c['ERROR']};
                          color: {c['ON_ACCENT']}; font-weight: bold; }}
    QPushButton#success:hover, QPushButton#danger:hover {{ border-color: {c['INDICATOR']}; }}
    QPushButton#primary:disabled, QPushButton#success:disabled,
    QPushButton#danger:disabled, QPushButton[primary="true"]:disabled {{
        background-color: {c['BG_LIGHTER']}; border-color: {c['BORDER']};
        color: {c['TEXT_DISABLED']}; }}

    QToolButton {{ background-color: transparent; border: 1px solid transparent;
                   border-radius: 6px; padding: 4px; color: {c['TEXT']}; }}
    QToolButton:hover {{ background-color: {c['HOVER']}; border-color: {c['BORDER']}; }}
    QToolButton:pressed {{ background-color: {c['SELECTION']}; }}
    QToolButton:checked {{ background-color: {c['SELECTION']}; border-color: {c['ACCENT']}; }}
    QToolButton::menu-indicator {{ image: none; width: 0; }}

    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateTimeEdit, QTimeEdit {{
        background-color: {c['SURFACE']}; border: 1px solid {c['BORDER']};
        border-radius: 6px; padding: {pad_v}px {pad_h - 2}px; color: {c['TEXT']};
        selection-background-color: {c['ACCENT']}; selection-color: {c['ON_ACCENT']};
        min-height: {row_h - 6}px; }}
    QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover {{
        border-color: {c['BORDER_LIGHT']}; }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
        border: 1px solid {c['ACCENT']}; }}
    QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled,
    QComboBox:disabled {{ background-color: {c['BG_LIGHTER']}; color: {c['TEXT_DISABLED']}; }}
    QLineEdit[readOnly="true"] {{ background-color: {c['BG_LIGHTER']}; }}
    QComboBox::drop-down {{ border: none; width: 22px; }}
    QComboBox::down-arrow {{ {down} width: 12px; height: 12px; }}
    QComboBox QAbstractItemView {{ background-color: {c['SURFACE']};
        border: 1px solid {c['BORDER_LIGHT']}; border-radius: 4px; padding: 2px;
        selection-background-color: {c['SELECTION']}; selection-color: {c['TEXT']};
        color: {c['TEXT']}; outline: 0; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button,
    QSpinBox::down-button, QDoubleSpinBox::down-button {{
        background-color: transparent; border: none; width: 16px; }}
    QSpinBox::up-button:hover, QDoubleSpinBox::up-button:hover,
    QSpinBox::down-button:hover, QDoubleSpinBox::down-button:hover {{
        background-color: {c['HOVER']}; border-radius: 3px; }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ {up} width: 10px; height: 10px; }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ {down} width: 10px; height: 10px; }}

    QCheckBox, QRadioButton {{ spacing: 8px; color: {c['TEXT']}; background: transparent; }}
    QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px;
        border: 1px solid {c['BORDER_LIGHT']}; background-color: {c['SURFACE']}; }}
    QCheckBox::indicator {{ border-radius: 4px; }}
    QRadioButton::indicator {{ border-radius: 9px; }}
    QCheckBox::indicator:hover, QRadioButton::indicator:hover {{ border-color: {c['ACCENT']}; }}
    QCheckBox::indicator:checked {{ background-color: {c['ACCENT']};
        border-color: {c['ACCENT']}; {check} }}
    QRadioButton::indicator:checked {{ background-color: {c['ACCENT']};
        border-color: {c['ACCENT']}; {dot} }}
    QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{
        background-color: {c['BG_LIGHTER']}; border-color: {c['BORDER']}; }}
    QCheckBox:disabled, QRadioButton:disabled {{ color: {c['TEXT_DISABLED']}; }}

    QSlider::groove:horizontal {{ height: 4px; background: {c['BORDER']}; border-radius: 2px; }}
    QSlider::sub-page:horizontal {{ background: {c['ACCENT']}; border-radius: 2px; }}
    QSlider::handle:horizontal {{ width: 16px; height: 16px; margin: -6px 0;
        background: {c['ON_ACCENT']}; border: 2px solid {c['ACCENT']}; border-radius: 10px; }}
    QSlider::handle:horizontal:hover {{ border-color: {c['INDICATOR']}; }}

    QProgressBar {{ background-color: {c['SURFACE']}; border: 1px solid {c['BORDER']};
                    border-radius: 6px; text-align: center; color: {c['TEXT']};
                    min-height: 16px; }}
    QProgressBar::chunk {{ background-color: {c['ACCENT']}; border-radius: 5px; }}

    QTableWidget, QTableView, QTreeView, QTreeWidget, QListView, QListWidget {{
        background-color: {c['BG_LIGHT']}; alternate-background-color: {c['BG_LIGHTER']};
        border: 1px solid {c['BORDER']}; border-radius: 6px;
        gridline-color: {c['BORDER']}; selection-background-color: {c['SELECTION']};
        selection-color: {c['TEXT']}; outline: 0; }}
    QTableWidget, QTableView {{ font-family: "{p.font_mono}", monospace; }}
    QTableView::item:selected, QTreeView::item:selected, QListView::item:selected {{
        background-color: {c['SELECTION']}; color: {c['TEXT']}; }}
    QTreeView::item, QListView::item {{ padding: {max(2, round(4 * density))}px; }}
    QTreeView::item:hover:!selected, QListView::item:hover:!selected {{
        background-color: {c['HOVER']}; }}
    QHeaderView {{ background-color: {c['BG_LIGHTER']}; border: none; }}
    QHeaderView::section {{ background-color: {c['BG_LIGHTER']}; border: none;
        border-right: 1px solid {c['BORDER']}; border-bottom: 1px solid {c['BORDER']};
        padding: {max(3, round(6 * density))}px; color: {c['ACCENT_LIGHT']};
        font-weight: bold; font-family: "{p.font_ui}"; }}
    QTableCornerButton::section {{ background-color: {c['BG_LIGHTER']}; border: none; }}

    QPlainTextEdit, QTextEdit, QTextBrowser {{ background-color: {c['BG_LIGHT']};
        color: {c['TEXT']}; border: 1px solid {c['BORDER']}; border-radius: 6px;
        selection-background-color: {c['ACCENT']}; selection-color: {c['ON_ACCENT']}; }}

    QTabWidget::pane {{ background-color: {c['BG_LIGHT']}; border: 1px solid {c['BORDER']};
                        border-radius: 8px; top: -1px; }}
    QTabBar {{ background: transparent; qproperty-drawBase: 0; }}
    QTabBar::tab {{ background-color: transparent; border: none;
                    border-bottom: 3px solid transparent;
                    padding: {max(4, round(8 * density))}px {max(8, round(16 * density))}px;
                    margin-right: 2px; color: {c['TEXT_DIM']}; }}
    QTabBar::tab:selected {{ color: {c['TEXT']}; border-bottom: 3px solid {c['INDICATOR']};
                             font-weight: 600; }}
    QTabBar::tab:hover:!selected {{ color: {c['TEXT']}; background-color: {c['HOVER']};
                                    border-top-left-radius: 6px; border-top-right-radius: 6px; }}
    QTabBar::tab:disabled {{ color: {c['TEXT_DISABLED']}; }}
    QTabBar::tab:left {{ border-bottom: none; border-left: 3px solid transparent;
                         padding: {max(8, round(14 * density))}px 6px; }}
    QTabBar::tab:left:selected {{ border-left: 3px solid {c['INDICATOR']}; }}
    QTabBar::tab:right {{ border-bottom: none; border-right: 3px solid transparent;
                         padding: {max(8, round(14 * density))}px 6px; }}
    QTabBar::tab:right:selected {{ border-right: 3px solid {c['INDICATOR']}; }}

    QToolBar {{ background-color: {c['BG']}; border: none;
                border-bottom: 1px solid {c['BORDER']};
                padding: {max(3, round(5 * density))}px 8px; spacing: 8px; }}
    QToolBar::separator {{ background-color: {c['BORDER']}; width: 1px; margin: 5px 8px; }}

    QStatusBar {{ background-color: {c['HEADER_BG']}; color: {c['HEADER_DIM']};
                  border-top: 1px solid {c['BORDER']}; }}
    QStatusBar QLabel {{ color: {c['HEADER_DIM']}; background: transparent; }}
    QStatusBar QToolButton {{ color: {c['HEADER_TEXT']}; padding: 2px 22px 2px 10px; }}
    QStatusBar QToolButton::menu-button {{ border: none; width: 18px; }}
    QStatusBar QToolButton:hover {{ background-color: {mix(c['HEADER_BG'], c['HEADER_TEXT'], 0.15)};
                                    border-color: transparent; }}
    QStatusBar::item {{ border: none; }}

    QMenuBar {{ background-color: {c['HEADER_BG']}; color: {c['HEADER_TEXT']};
                border-bottom: 1px solid {c['BORDER']}; padding: 2px; }}
    QMenuBar::item {{ padding: 5px 12px; border-radius: 4px; background: transparent;
                      color: {c['HEADER_TEXT']}; }}
    QMenuBar::item:selected {{ background-color: {mix(c['HEADER_BG'], c['HEADER_TEXT'], 0.16)}; }}
    QMenuBar::item:pressed {{ background-color: {c['ACCENT']}; color: {c['ON_ACCENT']}; }}
    QMenu {{ background-color: {c['SURFACE']}; border: 1px solid {c['BORDER_LIGHT']};
             border-radius: 6px; padding: 5px; }}
    QMenu::item {{ padding: 6px 28px 6px 14px; border-radius: 4px; background: transparent; }}
    QMenu::item:selected {{ background-color: {c['SELECTION']}; }}
    QMenu::item:disabled {{ color: {c['TEXT_DISABLED']}; }}
    QMenu::separator {{ height: 1px; background: {c['BORDER']}; margin: 5px 8px; }}
    QMenu::indicator {{ width: 14px; height: 14px; left: 6px; }}
    QMenu::indicator:checked {{ {check} background-color: {c['ACCENT']}; border-radius: 3px; }}
    QMenu::section {{ color: {c['TEXT_DIM']}; padding: 4px 12px; }}

    QToolTip {{ background-color: {c['SURFACE']}; color: {c['TEXT']};
                border: 1px solid {c['ACCENT']}; border-radius: 4px; padding: 5px 8px; }}

    QSplitter::handle {{ background-color: {c['BORDER']}; }}
    QSplitter::handle:vertical {{ height: 3px; }}
    QSplitter::handle:horizontal {{ width: 3px; }}
    QSplitter::handle:hover {{ background-color: {c['ACCENT']}; }}
    QMainWindow::separator {{ background: {c['BORDER']}; width: 3px; height: 3px; }}
    QMainWindow::separator:hover {{ background: {c['ACCENT']}; }}

    QDockWidget {{ color: {c['TEXT']}; font-weight: 600; }}
    QDockWidget::title {{ background-color: {c['BG_LIGHTER']}; padding: 6px 10px;
                          border-bottom: 1px solid {c['BORDER']}; text-align: left; }}

    QScrollBar:vertical {{ background: transparent; width: 11px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {c['BORDER_LIGHT']}; border-radius: 4px;
                                   min-height: 28px; margin: 2px; }}
    QScrollBar::handle:vertical:hover {{ background: {c['TEXT_DIM']}; }}
    QScrollBar:horizontal {{ background: transparent; height: 11px; margin: 0; }}
    QScrollBar::handle:horizontal {{ background: {c['BORDER_LIGHT']}; border-radius: 4px;
                                     min-width: 28px; margin: 2px; }}
    QScrollBar::handle:horizontal:hover {{ background: {c['TEXT_DIM']}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

    QDialogButtonBox QPushButton {{ min-width: 80px; }}

    /* themekit widgets */
    QWidget#brandBadge, QWidget#brandBadge QLabel {{ background: transparent; }}
    QLabel#brandTitle {{ font-family: {head}; font-size: {round(13 * scale)}pt;
                         font-weight: 700; color: {c['TEXT']}; letter-spacing: 0.5px; }}
    QLabel#brandSub {{ font-size: {fs_small}pt; color: {c['TEXT_DIM']}; }}
    QDialog#commandPalette {{ background-color: {c['SURFACE']};
                              border: 1px solid {c['ACCENT']}; border-radius: 10px; }}
    QDialog#commandPalette QLineEdit {{ font-size: {round(12 * scale)}pt; padding: 8px 10px;
                                        border-radius: 8px; }}
    QDialog#commandPalette QListWidget {{ background: {c['SURFACE']}; border: none; }}
    QDialog#commandPalette QListWidget::item {{ padding: 6px 8px; border-radius: 6px; }}
    QDialog#commandPalette QListWidget::item:selected {{ background: {c['SELECTION']}; }}
    QLabel#hint {{ color: {c['TEXT_DIM']}; font-size: {fs_small}pt; }}
    QStatusBar QLabel#hint {{ color: {c['HEADER_DIM']}; padding-right: 8px; }}
    """


# ════════════════════════════════════════════════════════════════════════
#  Theme manager
# ════════════════════════════════════════════════════════════════════════

class _ShowAdopter(QObject):
    """App event filter: the first time any top-level window is shown,
    re-skin its widgets that were styled from an un-synced (Classic)
    module — device panels built from their own theme copy, say."""

    def eventFilter(self, obj, ev):                    # noqa: N802
        if ev.type() == QEvent.Type.Show and isinstance(obj, QWidget) \
                and obj.isWindow() and not obj.property("_tk_adopted"):
            obj.setProperty("_tk_adopted", True)
            try:
                manager().adopt(obj)
            except Exception:                          # noqa: BLE001
                pass
        return False


class _Signals(QObject):
    changed = pyqtSignal(str)                # palette key, after apply


def _alive(obj) -> bool:
    try:
        from PyQt6 import sip
        return obj is not None and not sip.isdeleted(obj)
    except ImportError:                                # pragma: no cover
        return obj is not None


class ThemeManager:
    """Singleton (see :func:`manager`). Owns the active palette, scale and
    density, and pushes them into the running app on every change.

    Plain Python object so it outlives any one QApplication (test suites
    build several); its Qt signal holder is recreated if Qt deleted it."""

    def __init__(self):
        self._signals = _Signals()
        self.palette: Palette = PALETTES[DEFAULT_THEME]
        self.scale: float = 1.0
        self.density_key: str = "comfortable"
        self._sinks: List[Tuple[object, Dict[str, str]]] = []
        self._series_sinks: List[list] = []
        self._extra_css: List[Callable[[Palette, float, float], str]] = []
        self._settings: Optional[QSettings] = None
        self._applied_once = False
        self._adopter: Optional[_ShowAdopter] = None
        self._foreign: set = set()

    @property
    def changed(self):
        """``pyqtSignal(str)`` — emitted with the palette key after apply."""
        if not _alive(self._signals):
            self._signals = _Signals()
        return self._signals.changed

    # ── registration ──────────────────────────────────────────────────
    def register_sink(self, obj, aliases: Optional[Dict[str, str]] = None,
                      series_list: Optional[list] = None) -> None:
        """``obj`` (module or class) gets token values written into its
        attributes on every apply. ``aliases`` maps obj attr → token for
        legacy names (``TEXT_PRIMARY`` → ``TEXT``). ``series_list`` is a
        mutable list updated IN PLACE with the series colors."""
        self._sinks.append((obj, dict(aliases or {})))
        if series_list is not None:
            self._series_sinks.append(series_list)
        self._write_sink(obj, dict(aliases or {}))
        if series_list is not None:
            series_list[:] = list(self.palette.series)

    def add_stylesheet_extra(self, fn: Callable[[Palette, float, float], str]
                             ) -> None:
        """App-specific CSS appended after the shared sheet."""
        if fn not in self._extra_css:
            self._extra_css.append(fn)

    def _write_sink(self, obj, aliases: Dict[str, str]) -> None:
        tok = self.palette.tokens
        for name, value in tok.items():
            if hasattr(obj, name):
                try:
                    setattr(obj, name, value)
                except (AttributeError, TypeError):
                    pass
        for attr, token in aliases.items():
            if token in tok:
                try:
                    setattr(obj, attr, tok[token])
                except (AttributeError, TypeError):
                    pass

    def sync_foreign_modules(self) -> None:
        """Rewrite the palette into any loaded ``*.theme`` module that looks
        like a copy of the classic theme (the device apps' ``theme.py``),
        so panels they build from now on match the active theme."""
        tok = self.palette.tokens
        own = {id(o) for o, _ in self._sinks}
        for name, mod in list(sys.modules.items()):
            if mod is None or not name.split(".")[-1] in ("theme", "themes"):
                continue
            if id(mod) in own or not all(hasattr(mod, a)
                                         for a in ("BG", "TEXT", "ACCENT")):
                continue
            for t in TOKENS + tuple(ALIASES):
                if hasattr(mod, t):
                    try:
                        setattr(mod, t, tok[t])
                    except Exception:                  # noqa: BLE001
                        pass
            pal = getattr(mod, "PALETTE", None)
            if isinstance(pal, list) and pal:
                pal[:] = [self.palette.series_color(i) for i in range(len(pal))]
            self._foreign.add(name)

    # ── state ─────────────────────────────────────────────────────────
    @property
    def settings(self) -> Optional[QSettings]:
        """The persistence store handed to :meth:`init` (None in tests)."""
        return self._settings

    @property
    def density(self) -> float:
        return DENSITIES.get(self.density_key, 1.0)

    def stylesheet(self) -> str:
        css = build_stylesheet(self.palette, self.scale, self.density)
        for fn in self._extra_css:
            try:
                css += fn(self.palette, self.scale, self.density)
            except Exception:                          # noqa: BLE001
                pass
        return css

    def init(self, app: Optional[QApplication] = None,
             settings: Optional[QSettings] = None) -> None:
        """Entry-point setup: load the persisted choice (if ``settings``)
        and apply it. Window constructors call :meth:`ensure_applied`
        instead, which never touches persistence."""
        self._settings = settings
        key, scale, dens = DEFAULT_THEME, 1.0, "comfortable"
        if settings is not None:
            key = str(settings.value("appearance/theme", DEFAULT_THEME))
            try:
                scale = float(settings.value("appearance/scale", 1.0))
            except (TypeError, ValueError):
                scale = 1.0
            dens = str(settings.value("appearance/density", "comfortable"))
        if key not in PALETTES:
            key = LEGACY_KEYS.get(key, DEFAULT_THEME)
        self.scale = min(max(scale, SCALE_RANGE[0]), SCALE_RANGE[1])
        self.density_key = dens if dens in DENSITIES else "comfortable"
        self.apply(key, persist=False, force=True)

    def ensure_applied(self) -> None:
        """Apply once per QApplication (a NEW app has no stylesheet yet)."""
        app = QApplication.instance()
        if app is None:
            return
        if not self._applied_once or getattr(app, "_tk_key", None) is None:
            self.apply(self.palette.key, persist=False, force=True)

    def apply(self, key: Optional[str] = None, *, scale: Optional[float] = None,
              density: Optional[str] = None, persist: bool = True,
              force: bool = False) -> None:
        """Switch theme / scale / density live."""
        key = key or self.palette.key
        if key not in PALETTES:
            key = LEGACY_KEYS.get(key, DEFAULT_THEME)
        old = self.palette
        new = PALETTES[key]
        if scale is not None:
            self.scale = min(max(float(scale), SCALE_RANGE[0]), SCALE_RANGE[1])
        if density is not None and density in DENSITIES:
            self.density_key = density
        if (not force and new is old and scale is None and density is None
                and self._applied_once):
            return
        self.palette = new
        for obj, aliases in self._sinks:
            self._write_sink(obj, aliases)
        for lst in self._series_sinks:
            lst[:] = list(new.series)
        self.sync_foreign_modules()

        app = QApplication.instance()
        if app is not None:
            if not _alive(self._adopter) or self._adopter.parent() is not app:
                self._adopter = _ShowAdopter(app)
                app.installEventFilter(self._adopter)
            f = QFont(new.font_ui)
            f.setPointSizeF(10 * self.scale)
            app.setFont(f)
            app.setStyleSheet(self.stylesheet())
            app._tk_key = new.key
            self._apply_pyqtgraph_config()
            self._apply_mpl_rc()
            mapping = color_map(old, new, extra_sources=(CLASSIC,))
            if mapping:
                widgets = app.allWidgets()
                for w in widgets:
                    try:
                        remap_widget(w, mapping)
                    except RuntimeError:               # deleted C++ object
                        pass
                restyle_pyqtgraph(widgets, mapping, new)
            for w in app.allWidgets():
                try:
                    w.update()
                except RuntimeError:
                    pass
        self._applied_once = True
        if persist:
            self._persist()
        self.changed.emit(new.key)

    def adopt(self, root: QWidget) -> None:
        """Re-skin a freshly shown subtree whose styles came from the
        Classic palette (un-synced legacy modules): stylesheets, label rich
        text and pyqtgraph items. Matplotlib canvases are left to their
        owners (see :meth:`apply`)."""
        self.sync_foreign_modules()
        if self.palette is CLASSIC:
            return
        mapping = color_map(CLASSIC, self.palette)
        widgets = [root, *root.findChildren(QWidget)]
        for w in widgets:
            try:
                remap_widget(w, mapping)
            except RuntimeError:
                pass
        restyle_pyqtgraph(widgets, mapping, self.palette)

    def toggle_light_dark(self) -> None:
        self.apply(self.palette.pair)

    def zoom(self, delta: float) -> None:
        self.apply(scale=round(self.scale + delta, 2))

    def _persist(self) -> None:
        if self._settings is None:
            return
        self._settings.setValue("appearance/theme", self.palette.key)
        self._settings.setValue("appearance/scale", self.scale)
        self._settings.setValue("appearance/density", self.density_key)
        self._settings.sync()

    def _apply_pyqtgraph_config(self) -> None:
        pg = sys.modules.get("pyqtgraph")
        if pg is None:
            return
        pg.setConfigOption("background", self.palette["PLOT_BG"])
        pg.setConfigOption("foreground", self.palette["TEXT_DIM"])
        pg.setConfigOption("antialias", True)

    def _apply_mpl_rc(self) -> None:
        if "matplotlib" not in sys.modules:
            return
        try:
            import matplotlib
            matplotlib.rcParams.update(mpl_rc(self.palette, self.scale))
        except Exception:                              # noqa: BLE001
            pass


#: old ``appearance/theme`` values written by earlier app versions
LEGACY_KEYS = {"dark": "classic_dark", "light": "classic_light"}

_MANAGER: Optional[ThemeManager] = None


def manager() -> ThemeManager:
    global _MANAGER
    if _MANAGER is None:
        _MANAGER = ThemeManager()
    return _MANAGER


def current() -> Palette:
    return manager().palette


# ════════════════════════════════════════════════════════════════════════
#  Plot restyling
# ════════════════════════════════════════════════════════════════════════

def mpl_rc(p: Palette, scale: float = 1.0) -> Dict[str, object]:
    c = p.tokens
    return {
        "figure.facecolor": c["PLOT_FIG"], "figure.edgecolor": c["PLOT_FIG"],
        "axes.facecolor": c["PLOT_BG"], "axes.edgecolor": c["BORDER_LIGHT"],
        "axes.labelcolor": c["TEXT"], "axes.titlecolor": c["TEXT"],
        "axes.grid": True, "axes.axisbelow": True,
        "grid.color": c["AXIS"], "grid.linestyle": "-",
        "grid.linewidth": 0.5, "grid.alpha": 0.45,
        "text.color": c["TEXT"], "xtick.color": c["TEXT_DIM"],
        "ytick.color": c["TEXT_DIM"], "xtick.labelcolor": c["TEXT"],
        "ytick.labelcolor": c["TEXT"],
        "legend.facecolor": c["SURFACE"], "legend.edgecolor": c["BORDER"],
        "legend.labelcolor": c["TEXT"],
        "savefig.facecolor": c["PLOT_FIG"], "savefig.edgecolor": c["PLOT_FIG"],
    }


def restyle_pyqtgraph(widgets: Iterable[QWidget], mapping: Dict[str, str],
                      p: Palette) -> None:
    """Swap palette colors on existing pyqtgraph views and items."""
    pg = sys.modules.get("pyqtgraph")
    if pg is None or not mapping:
        return

    def pen_of(spec):
        try:
            return pg.mkPen(spec)
        except Exception:                              # noqa: BLE001
            return None

    def brush_of(spec):
        try:
            return pg.mkBrush(spec)
        except Exception:                              # noqa: BLE001
            return None

    def mapped_pen(spec):
        if spec is None:
            return None
        pen = pen_of(spec)
        if pen is None:
            return None
        col = _remap_qcolor(pen.color(), mapping)
        if col is None:
            return None
        pen = QPen(pen)
        pen.setColor(col)
        return pen

    def mapped_brush(spec):
        if spec is None:
            return None
        br = brush_of(spec)
        if br is None or br.style() == Qt.BrushStyle.NoBrush:
            return None
        col = _remap_qcolor(br.color(), mapping)
        if col is None:
            return None
        br = QBrush(br)
        br.setColor(col)
        return br

    for w in widgets:
        try:
            if not isinstance(w, pg.GraphicsView):
                continue
            bg = w.backgroundBrush().color()
            nbg = _remap_qcolor(bg, mapping)
            if nbg is not None:
                w.setBackground(nbg)
            scene = w.scene()
            if scene is None:
                continue
            for item in scene.items():
                _restyle_pg_item(pg, item, mapped_pen, mapped_brush, mapping)
        except RuntimeError:
            continue


def _restyle_pg_item(pg, item, mapped_pen, mapped_brush, mapping) -> None:
    try:
        if isinstance(item, pg.AxisItem):
            np_ = mapped_pen(item.pen())
            if np_ is not None:
                item.setPen(np_)
            tp = mapped_pen(item.textPen())
            if tp is not None:
                item.setTextPen(tp)
            lbl = getattr(item, "labelStyle", None)
            if isinstance(lbl, dict) and "color" in lbl:
                c = _remap_qcolor(QColor(lbl["color"]), mapping)
                if c is not None:
                    lbl["color"] = c.name()
                    item._updateLabel()
        elif isinstance(item, pg.PlotDataItem):
            o = item.opts
            np_ = mapped_pen(o.get("pen"))
            if np_ is not None:
                item.setPen(np_)
            sp = mapped_pen(o.get("symbolPen"))
            if sp is not None:
                item.setSymbolPen(sp)
            sb = mapped_brush(o.get("symbolBrush"))
            if sb is not None:
                item.setSymbolBrush(sb)
            fb = mapped_brush(o.get("fillBrush"))
            if fb is not None:
                item.setFillBrush(fb)
        elif isinstance(item, pg.PlotCurveItem):
            np_ = mapped_pen(item.opts.get("pen"))
            if np_ is not None:
                item.setPen(np_)
            br = mapped_brush(item.opts.get("brush"))
            if br is not None:
                item.setBrush(br)
        elif isinstance(item, pg.ScatterPlotItem):
            np_ = mapped_pen(item.opts.get("pen"))
            if np_ is not None:
                item.setPen(np_)
            br = mapped_brush(item.opts.get("brush"))
            if br is not None:
                item.setBrush(br)
        elif isinstance(item, pg.InfiniteLine):
            np_ = mapped_pen(item.pen)
            if np_ is not None:
                item.setPen(np_)
        elif isinstance(item, pg.LegendItem):
            col = item.opts.get("labelTextColor")
            if col is not None:
                c = _remap_qcolor(pg.mkColor(col), mapping)
                if c is not None:
                    item.setLabelTextColor(c)
            np_ = mapped_pen(item.opts.get("pen"))
            if np_ is not None:
                item.setPen(np_)
            br = mapped_brush(item.opts.get("brush"))
            if br is not None:
                item.setBrush(br)
        elif isinstance(item, pg.TextItem):
            c = _remap_qcolor(item.color, mapping) if hasattr(item, "color") \
                else None
            if c is not None:
                item.setColor(c)
        elif isinstance(item, pg.LabelItem):
            col = item.opts.get("color")
            if col is not None:
                c = _remap_qcolor(pg.mkColor(col), mapping)
                if c is not None:
                    item.setText(item.text, color=c.name())
        elif isinstance(item, pg.ViewBox):
            br = item.background.brush() if hasattr(item, "background") \
                else None
            if br is not None and br.style() != Qt.BrushStyle.NoBrush:
                c = _remap_qcolor(br.color(), mapping)
                if c is not None:
                    item.setBackgroundColor(c)
    except Exception:                                  # noqa: BLE001
        pass


# ════════════════════════════════════════════════════════════════════════
#  Icons and branding
# ════════════════════════════════════════════════════════════════════════

class _SvgIconEngine(QIconEngine):
    """Renders an SVG template in the CURRENT theme color at paint time,
    so toolbar/menu icons follow live theme switches."""

    def __init__(self, template: str, token: str = "TEXT"):
        super().__init__()
        self._template = template
        self._token = token

    def _color(self, mode) -> str:
        p = current()
        if mode == QIcon.Mode.Disabled:
            return p["TEXT_DISABLED"]
        if mode == QIcon.Mode.Selected:
            return p["ON_ACCENT"]
        return p.get(self._token, self._token)

    def paint(self, painter, rect, mode, state):       # noqa: D401
        from PyQt6.QtSvg import QSvgRenderer
        svg = self._template.replace("{color}", self._color(mode))
        r = QSvgRenderer(QByteArray(svg.encode("utf-8")))
        r.render(painter, QRectF(rect))

    def pixmap(self, size, mode, state):
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size, mode, state, scale):  # noqa: N802
        s = max(1.0, float(scale))
        pm = QPixmap(QSize(round(size.width() * s), round(size.height() * s)))
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        self.paint(p, QRect(QPoint(0, 0), pm.size()), mode, state)
        p.end()
        pm.setDevicePixelRatio(s)
        return pm

    def clone(self):
        return _SvgIconEngine(self._template, self._token)


def themed_svg_icon(template: str, token: str = "TEXT") -> QIcon:
    """``template`` is SVG with ``{color}`` placeholders; ``token`` is a
    palette token (or a literal hex) — re-resolved on every paint."""
    return QIcon(_SvgIconEngine(template, token))


def _asset(name: str) -> Path:
    return ASSETS / name


def logo_pixmap(name: str = "dfan-aeronautics.png", height: int = 32,
                plate: Optional[bool] = None, radius: Optional[float] = None,
                dpr: float = 2.0) -> QPixmap:
    """Brand image scaled to ``height`` px (logical). ``plate`` draws a white
    rounded backing (round for the roundel) so the uncolored mark reads on
    dark surfaces; default = plate only when the active theme is dark."""
    path = _asset(name)
    if plate is None:
        plate = current().dark
    src = QPixmap()
    if path.suffix.lower() == ".svg":
        from PyQt6.QtSvg import QSvgRenderer
        r = QSvgRenderer(str(path))
        vs = r.defaultSize()
        w = max(1, round(height * vs.width() / max(1, vs.height())))
        src = QPixmap(round(w * dpr), round(height * dpr))
        src.fill(Qt.GlobalColor.transparent)
        p = QPainter(src)
        r.render(p)
        p.end()
    elif path.is_file():
        src.load(str(path))
    if src.isNull():
        return QPixmap()
    h_px = round(height * dpr)
    roundel = name.startswith("dfan")
    pad = 0 if (roundel or not plate) else round(h_px * 0.14)
    img = src.scaledToHeight(h_px - 2 * pad,
                             Qt.TransformationMode.SmoothTransformation)
    out = QPixmap(img.width() + 2 * pad, h_px)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    if plate:
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor("#ffffff"))
        if roundel:
            p.drawEllipse(QRectF(1, 1, out.width() - 2, out.height() - 2))
        else:
            rr = radius if radius is not None else h_px * 0.18
            p.drawRoundedRect(QRectF(0, 0, out.width(), out.height()), rr, rr)
    p.drawPixmap(pad, pad, img)
    p.end()
    out.setDevicePixelRatio(dpr)
    return out


def app_icon() -> QIcon:
    """Window/taskbar icon: the Aeronautics roundel on a white disc."""
    icon = QIcon()
    for s in (16, 24, 32, 48, 64, 128, 256):
        pm = logo_pixmap("dfan-aeronautics.png", s, plate=True, dpr=1.0)
        if not pm.isNull():
            icon.addPixmap(pm)
    return icon


class BrandBadge(QWidget):
    """Roundel + app title + org line for the left end of a toolbar."""

    def __init__(self, title: str, subtitle: str = ORG_LINE,
                 height: int = 34, parent=None):
        super().__init__(parent)
        self.setObjectName("brandBadge")
        self._h = height
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 10, 0)
        lay.setSpacing(9)
        self.logo = QLabel()
        self.logo.setToolTip(ORG_LINE)
        lay.addWidget(self.logo)
        col = QVBoxLayout()
        col.setSpacing(0)
        col.setContentsMargins(0, 0, 0, 0)
        self.title = QLabel(title)
        self.title.setObjectName("brandTitle")
        self.sub = QLabel(subtitle)
        self.sub.setObjectName("brandSub")
        col.addWidget(self.title)
        if subtitle:
            col.addWidget(self.sub)
        lay.addLayout(col)
        self._refresh()
        manager().changed.connect(self._refresh)

    def _refresh(self, *_):
        try:
            self.logo.setPixmap(logo_pixmap("dfan-aeronautics.png", self._h,
                                            plate=True))
        except RuntimeError:
            pass


def make_splash_pixmap(app_name: str, version: str,
                       tagline: str = "", width: int = 560,
                       height: int = 330) -> QPixmap:
    """Brand splash: white plate with the USAFA wordmark + Aeronautics
    roundel above a Class Royal band carrying the app name."""
    p = current()
    dpr = 2.0
    pm = QPixmap(round(width * dpr), round(height * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    g = QPainter(pm)
    g.setRenderHint(QPainter.RenderHint.Antialiasing)
    g.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, width, height), 14, 14)
    g.setClipPath(path)
    g.fillRect(QRectF(0, 0, width, height), QColor("#ffffff"))
    band_top = height * 0.50
    grad = QLinearGradient(0, band_top, 0, height)
    grad.setColorAt(0, QColor(USAFA["class_royal"]))
    grad.setColorAt(1, QColor("#001634"))
    g.fillRect(QRectF(0, band_top, width, height - band_top), QBrush(grad))
    g.fillRect(QRectF(0, band_top, width, 4), QColor(USAFA["class_yellow"]))
    # logos on the white field (uncolored, generous clear space)
    roundel = logo_pixmap("dfan-aeronautics.png", 112, plate=False)
    wm = logo_pixmap("wordmark-horizontal.png", 86, plate=False)
    if not roundel.isNull():
        g.drawPixmap(QPoint(28, 24), roundel)
    if not wm.isNull():
        x = width - 28 - wm.width() / wm.devicePixelRatio()
        g.drawPixmap(QPoint(round(x), 36), wm)
    g.setPen(QColor("#ffffff"))
    f = QFont("Trebuchet MS", 26, QFont.Weight.Bold)
    g.setFont(f)
    g.drawText(QRectF(32, band_top + 18, width - 64, 46),
               Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
               app_name)
    g.setPen(QColor(USAFA["class_yellow"]))
    g.setFont(QFont("Segoe UI", 10, QFont.Weight.DemiBold))
    g.drawText(QRectF(32, band_top + 64, width - 64, 22),
               Qt.AlignmentFlag.AlignLeft, f"Version {version}")
    g.setPen(QColor("#c8d3e6"))
    g.setFont(QFont("Segoe UI", 9))
    if tagline:
        g.drawText(QRectF(32, band_top + 88, width - 64, 20),
                   Qt.AlignmentFlag.AlignLeft, tagline)
    g.drawText(QRectF(32, height - 30, width - 64, 20),
               Qt.AlignmentFlag.AlignLeft, ORG_LINE)
    g.end()
    _ = p
    return pm


# ════════════════════════════════════════════════════════════════════════
#  Appearance dialog
# ════════════════════════════════════════════════════════════════════════

class ThemeCard(QAbstractButton):
    """Checkable mini-window preview painted in its own palette."""

    def __init__(self, palette: Palette, parent=None):
        super().__init__(parent)
        self.palette_ = palette
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(palette.description)
        self.setFixedSize(196, 150)

    def sizeHint(self):                                 # noqa: N802
        return QSize(196, 150)

    def paintEvent(self, _ev):                         # noqa: N802
        c = self.palette_.tokens
        cur = current()
        g = QPainter(self)
        g.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        # selection ring in the ACTIVE theme so it reads on the dialog
        ring = QColor(cur["INDICATOR"] if self.isChecked() else
                      (cur["ACCENT"] if self.underMouse() else cur["BORDER"]))
        g.setPen(QPen(ring, 3 if self.isChecked() else 1.2))
        g.setBrush(QColor(c["BG"]))
        g.drawRoundedRect(r, 10, 10)
        inner = r.adjusted(8, 8, -8, -34)
        path = QPainterPath()
        path.addRoundedRect(inner, 6, 6)
        g.save()
        g.setClipPath(path)
        g.fillRect(inner, QColor(c["BG"]))
        hb = QRectF(inner.left(), inner.top(), inner.width(), 14)
        g.fillRect(hb, QColor(c["HEADER_BG"]))
        g.setBrush(QColor("#ffffff"))
        g.setPen(Qt.PenStyle.NoPen)
        g.drawEllipse(QRectF(hb.left() + 4, hb.top() + 2.5, 9, 9))
        g.fillRect(QRectF(hb.left() + 18, hb.top() + 5.5, 34, 3),
                   QColor(c["HEADER_TEXT"]))
        rail = QRectF(inner.left() + 4, hb.bottom() + 4, 38,
                      inner.height() - 22)
        g.setBrush(QColor(c["BG_LIGHT"]))
        g.setPen(QPen(QColor(c["BORDER"]), 1))
        g.drawRoundedRect(rail, 3, 3)
        for i in range(3):
            g.fillRect(QRectF(rail.left() + 5, rail.top() + 6 + i * 11, 26, 4),
                       QColor(c["TEXT_DIM"] if i else c["TEXT"]))
        plot = QRectF(rail.right() + 5, rail.top(),
                      inner.right() - rail.right() - 9, rail.height())
        g.setBrush(QColor(c["PLOT_BG"]))
        g.drawRoundedRect(plot, 3, 3)
        g.setPen(QPen(QColor(c["GRID"]), 1))
        for i in range(1, 4):
            y = plot.top() + plot.height() * i / 4
            g.drawLine(int(plot.left() + 3), int(y), int(plot.right() - 3), int(y))
        import math
        for s in range(3):
            g.setPen(QPen(QColor(self.palette_.series_color(s)), 1.8))
            pts = []
            for k in range(16):
                x = plot.left() + 4 + (plot.width() - 8) * k / 15
                y = plot.center().y() + math.sin(k / 2.4 + s * 1.7) * \
                    plot.height() * 0.22 + (s - 1) * 7
                pts.append((x, y))
            for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
                g.drawLine(int(x0), int(y0), int(x1), int(y1))
        btn = QRectF(plot.right() - 30, plot.bottom() - 13, 26, 9)
        g.setPen(Qt.PenStyle.NoPen)
        g.setBrush(QColor(c["ACCENT"]))
        g.drawRoundedRect(btn, 3, 3)
        g.fillRect(QRectF(plot.left() + 6, plot.bottom() - 6, 22, 3),
                   QColor(c["INDICATOR"]))
        g.restore()
        g.setPen(QColor(c["TEXT"]))
        f = QFont(self.palette_.font_heading)
        f.setPointSizeF(10)
        f.setBold(True)
        g.setFont(f)
        g.drawText(QRectF(r.left() + 12, r.bottom() - 28, r.width() - 24, 22),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   self.palette_.name)
        sw = 9
        for i, tok in enumerate(("ACCENT", "INDICATOR", "SUCCESS", "ERROR")):
            g.setPen(Qt.PenStyle.NoPen)
            g.setBrush(QColor(c[tok]))
            g.drawEllipse(QRectF(r.right() - 14 - (3 - i) * (sw + 3) - sw,
                                 r.bottom() - 21.5, sw, sw))
        g.end()

    def enterEvent(self, e):                           # noqa: N802
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):                           # noqa: N802
        self.update()
        super().leaveEvent(e)


class AppearanceDialog(QDialog):
    """Theme cards + UI scale + density, applied live; Cancel reverts."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Appearance")
        m = manager()
        self._orig = (m.palette.key, m.scale, m.density_key)
        v = QVBoxLayout(self)
        v.setSpacing(12)
        v.setContentsMargins(18, 16, 18, 14)

        head = QHBoxLayout()
        self._logo = QLabel()
        self._logo.setPixmap(logo_pixmap("dfan-aeronautics.png", 40,
                                         plate=True))
        head.addWidget(self._logo)
        tl = QVBoxLayout()
        t = QLabel("Appearance")
        t.setProperty("role", "heading")
        tl.addWidget(t)
        s = QLabel("Changes apply instantly. Cancel restores the previous "
                   "look.")
        s.setObjectName("hint")
        tl.addWidget(s)
        head.addLayout(tl, 1)
        v.addLayout(head)

        grid = QGridLayout()
        grid.setSpacing(10)
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self.cards: Dict[str, ThemeCard] = {}
        for i, p in enumerate(PALETTES.values()):
            card = ThemeCard(p)
            card.setChecked(p.key == m.palette.key)
            card.clicked.connect(lambda _=False, k=p.key: m.apply(k))
            self._group.addButton(card)
            self.cards[p.key] = card
            grid.addWidget(card, i // 3, i % 3)
        v.addLayout(grid)

        form = QGridLayout()
        form.setHorizontalSpacing(12)
        form.addWidget(QLabel("UI scale"), 0, 0)
        self.scale_slider = QSlider(Qt.Orientation.Horizontal)
        self.scale_slider.setRange(round(SCALE_RANGE[0] * 100),
                                   round(SCALE_RANGE[1] * 100))
        self.scale_slider.setSingleStep(5)
        self.scale_slider.setPageStep(10)
        self.scale_slider.setValue(round(m.scale * 100))
        self.scale_lbl = QLabel(f"{round(m.scale * 100)} %")
        self.scale_lbl.setMinimumWidth(48)
        # apply on release (a full restyle per pixel of drag is too much)
        self.scale_slider.valueChanged.connect(
            lambda val: self.scale_lbl.setText(f"{val} %"))
        self.scale_slider.sliderReleased.connect(self._apply_scale)
        self.scale_slider.actionTriggered.connect(
            lambda *_: QTimer.singleShot(0, self._apply_scale_if_idle))
        form.addWidget(self.scale_slider, 0, 1)
        form.addWidget(self.scale_lbl, 0, 2)
        form.addWidget(QLabel("Density"), 1, 0)
        self.density_combo = QComboBox()
        for k in DENSITIES:
            self.density_combo.addItem(k.capitalize(), k)
        self.density_combo.setCurrentIndex(
            list(DENSITIES).index(m.density_key))
        self.density_combo.currentIndexChanged.connect(
            lambda _i: m.apply(density=self.density_combo.currentData()))
        form.addWidget(self.density_combo, 1, 1)
        hint = QLabel("Shortcuts: Ctrl+= / Ctrl+- zoom, Ctrl+0 reset, "
                      "Ctrl+Shift+L light/dark, Ctrl+K command palette, "
                      "F11 full screen")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        form.addWidget(hint, 2, 0, 1, 3)
        v.addLayout(form)

        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                              | QDialogButtonBox.StandardButton.Cancel
                              | QDialogButtonBox.StandardButton.RestoreDefaults)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        bb.button(QDialogButtonBox.StandardButton.RestoreDefaults) \
            .clicked.connect(self._defaults)
        v.addWidget(bb)
        m.changed.connect(self._sync)

    def _apply_scale(self):
        manager().apply(scale=self.scale_slider.value() / 100)

    def _apply_scale_if_idle(self):
        if not self.scale_slider.isSliderDown():
            self._apply_scale()

    def _defaults(self):
        manager().apply(DEFAULT_THEME, scale=1.0, density="comfortable")

    def _sync(self, key: str):
        try:
            m = manager()
            for k, card in self.cards.items():
                card.setChecked(k == key)
                card.update()
            self.scale_slider.blockSignals(True)
            self.scale_slider.setValue(round(m.scale * 100))
            self.scale_slider.blockSignals(False)
            self.scale_lbl.setText(f"{round(m.scale * 100)} %")
            self.density_combo.blockSignals(True)
            self.density_combo.setCurrentIndex(
                list(DENSITIES).index(m.density_key))
            self.density_combo.blockSignals(False)
            self._logo.setPixmap(logo_pixmap("dfan-aeronautics.png", 40,
                                             plate=True))
        except RuntimeError:
            pass

    def reject(self):
        k, s, d = self._orig
        manager().apply(k, scale=s, density=d)
        super().reject()

    def done(self, r):
        try:
            manager().changed.disconnect(self._sync)
        except (TypeError, RuntimeError):
            pass
        super().done(r)


# ════════════════════════════════════════════════════════════════════════
#  Command palette
# ════════════════════════════════════════════════════════════════════════

def _strip_amp(text: str) -> str:
    return text.replace("&&", "\0").replace("&", "").replace("\0", "&")


def collect_actions(window: QMainWindow) -> List[Tuple[str, QAction]]:
    """``(path, action)`` for every leaf action in the menu bar."""
    out: List[Tuple[str, QAction]] = []
    seen = set()

    def walk(menu: QMenu, prefix: str):
        try:
            menu.aboutToShow.emit()           # let dynamic menus fill in
        except Exception:                              # noqa: BLE001
            pass
        for act in menu.actions():
            if act.isSeparator() or not act.isVisible():
                continue
            text = _strip_amp(act.text()).rstrip("…").strip()
            if act.menu() is not None:
                walk(act.menu(), f"{prefix}{text} ▸ ")
                continue
            if not text or id(act) in seen:
                continue
            seen.add(id(act))
            out.append((prefix + text, act))

    bar = window.menuBar()
    for act in bar.actions():
        if act.menu() is not None:
            walk(act.menu(), _strip_amp(act.text()) + " ▸ ")
    return out


def fuzzy_score(query: str, text: str) -> Optional[int]:
    """Subsequence match score (higher is better), None = no match."""
    q = query.lower().strip()
    t = text.lower()
    if not q:
        return 0
    if q in t:
        return 1000 - t.index(q) * 2 - len(t)
    score, pos, streak = 0, 0, 0
    for ch in q:
        if ch == " ":
            continue
        i = t.find(ch, pos)
        if i < 0:
            return None
        streak = streak + 1 if i == pos else 0
        score += 10 + streak * 6 - (i - pos)
        if i == 0 or t[i - 1] in " ▸-_/(":
            score += 12                       # word-start bonus
        pos = i + 1
    return score - len(t) // 4


class CommandPalette(QDialog):
    """Ctrl+K: type to find any menu command, Enter to run it."""

    def __init__(self, window: QMainWindow):
        super().__init__(window, Qt.WindowType.Popup
                         | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("commandPalette")
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._window = window
        self._actions = collect_actions(window)
        v = QVBoxLayout(self)
        v.setContentsMargins(10, 10, 10, 8)
        v.setSpacing(6)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Type a command…  (↑↓ to choose, Enter "
                                     "to run, Esc to close)")
        self.edit.setClearButtonEnabled(True)
        v.addWidget(self.edit)
        self.list = QListWidget()
        self.list.setUniformItemSizes(True)
        v.addWidget(self.list, 1)
        self.hint = QLabel()
        self.hint.setObjectName("hint")
        v.addWidget(self.hint)
        self.edit.textChanged.connect(self._filter)
        self.edit.installEventFilter(self)
        self.list.itemActivated.connect(self._run)
        self._filter("")
        w = max(560, window.width() // 2)
        self.resize(w, 420)
        geo = window.geometry()
        self.move(geo.center().x() - w // 2, geo.top() + 80)
        self.edit.setFocus()

    def _filter(self, text: str):
        self.list.clear()
        scored = []
        for path, act in self._actions:
            sc = fuzzy_score(text, path)
            if sc is not None:
                scored.append((sc, path, act))
        scored.sort(key=lambda t: (-t[0], t[1]))
        for _sc, path, act in scored[:200]:
            sc_txt = act.shortcut().toString(
                QKeySequence.SequenceFormat.NativeText)
            item = QListWidgetItem(path + (f"      {sc_txt}" if sc_txt else ""))
            item.setData(Qt.ItemDataRole.UserRole, act)
            if not act.isEnabled():
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
            if act.toolTip() and act.toolTip() != _strip_amp(act.text()):
                item.setToolTip(act.toolTip())
            self.list.addItem(item)
        for i in range(self.list.count()):
            if self.list.item(i).flags() & Qt.ItemFlag.ItemIsEnabled:
                self.list.setCurrentRow(i)
                break
        self.hint.setText(f"{len(scored)} of {len(self._actions)} commands")

    def eventFilter(self, obj, ev):                    # noqa: N802
        if obj is self.edit and ev.type() == QEvent.Type.KeyPress:
            k = ev.key()
            if k in (Qt.Key.Key_Down, Qt.Key.Key_Up, Qt.Key.Key_PageDown,
                     Qt.Key.Key_PageUp):
                QApplication.sendEvent(self.list, ev)
                return True
            if k in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._run(self.list.currentItem())
                return True
            if k == Qt.Key.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, ev)

    def _run(self, item):
        if item is None:
            return
        act = item.data(Qt.ItemDataRole.UserRole)
        if act is None or not act.isEnabled():
            return
        self.close()
        QTimer.singleShot(0, act.trigger)


# ════════════════════════════════════════════════════════════════════════
#  View menu / status-bar toggle
# ════════════════════════════════════════════════════════════════════════

class ThemeToggleButton(QToolButton):
    """Status-bar chip: click flips light/dark, menu arrow picks any theme."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        self.setAutoRaise(True)
        self.setMenu(build_theme_menu(self))
        self.clicked.connect(lambda: manager().toggle_light_dark())
        manager().changed.connect(self._refresh)
        self._refresh()

    def _refresh(self, *_):
        try:
            p = current()
            self.setText(("☾  " if p.dark else "☀  ") + p.name)
            self.setToolTip(f"Theme: {p.name} — click to switch to "
                            f"{PALETTES[p.pair].name} (Ctrl+Shift+L)")
        except RuntimeError:
            pass


def build_theme_menu(parent: QWidget, title: str = "&Theme") -> QMenu:
    menu = QMenu(title, parent)
    group = QActionGroup(menu)
    group.setExclusive(True)
    acts: Dict[str, QAction] = {}
    for p in PALETTES.values():
        a = QAction(p.name, menu)
        a.setCheckable(True)
        a.setToolTip(p.description)
        a.triggered.connect(lambda _=False, k=p.key: manager().apply(k))
        group.addAction(a)
        menu.addAction(a)
        acts[p.key] = a

    def sync(key):
        for k, a in acts.items():
            try:
                a.setChecked(k == key)
            except RuntimeError:
                pass
    sync(current().key)
    manager().changed.connect(sync)
    return menu


def install_view_menu(window: QMainWindow, view_menu: Optional[QMenu] = None,
                      docks: Sequence = ()) -> QMenu:
    """Add theme/appearance/zoom/full-screen/command-palette entries to
    ``view_menu`` (created as "&View" if None). ``docks`` (QDockWidgets)
    get show/hide toggles. Returns the menu."""
    if view_menu is None:
        view_menu = window.menuBar().addMenu("&View")
    elif view_menu.actions():
        view_menu.addSeparator()

    view_menu.addMenu(build_theme_menu(window))
    act = QAction("&Appearance…", window)
    act.setToolTip("Theme, UI scale and density")
    act.triggered.connect(lambda: AppearanceDialog(window).exec())
    view_menu.addAction(act)
    act = QAction("Toggle &Light / Dark", window)
    act.setShortcut(QKeySequence("Ctrl+Shift+L"))
    act.triggered.connect(lambda: manager().toggle_light_dark())
    view_menu.addAction(act)
    window.addAction(act)

    view_menu.addSeparator()
    for text, keys, fn in (
            ("Zoom &In", ("Ctrl+=", "Ctrl++"), lambda: manager().zoom(+0.1)),
            ("Zoom &Out", ("Ctrl+-",), lambda: manager().zoom(-0.1)),
            ("&Reset Zoom", ("Ctrl+0",), lambda: manager().apply(scale=1.0))):
        a = QAction(text, window)
        a.setShortcuts([QKeySequence(k) for k in keys])
        a.triggered.connect(fn)
        view_menu.addAction(a)
        window.addAction(a)

    view_menu.addSeparator()
    if docks:
        for d in docks:
            ta = d.toggleViewAction()
            view_menu.addAction(ta)
        view_menu.addSeparator()
    fs = QAction("&Full Screen", window)
    fs.setCheckable(True)
    fs.setShortcut(QKeySequence("F11"))

    def _fs(on):
        if on:
            window._tk_was_max = window.isMaximized()
            window.showFullScreen()
        else:
            (window.showMaximized if getattr(window, "_tk_was_max", False)
             else window.showNormal)()
    fs.toggled.connect(_fs)
    view_menu.addAction(fs)
    window.addAction(fs)
    cp = QAction("&Command Palette…", window)
    cp.setShortcuts([QKeySequence("Ctrl+K"), QKeySequence("Ctrl+Shift+P")])
    cp.triggered.connect(lambda: CommandPalette(window).show())
    view_menu.addAction(cp)
    window.addAction(cp)
    return view_menu
