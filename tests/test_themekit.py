"""Theme engine: palettes, live switching, remaps, view tooling, branding."""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # BEFORE PyQt6

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PyQt6.QtGui import QAction                       # noqa: E402
from PyQt6.QtWidgets import (QApplication, QLabel, QMainWindow,  # noqa: E402
                             QWidget)

from freestream import theme, themekit as tk          # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([sys.argv[0]])


@pytest.fixture(autouse=True)
def _restore_theme(app):
    """Every test leaves the default theme active, at 100 %."""
    yield
    tk.manager().apply(tk.DEFAULT_THEME, scale=1.0, density="comfortable",
                       persist=False, force=True)


def _lum(h):
    h = h.lstrip("#")
    rgb = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    f = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
         for c in rgb]
    return 0.2126 * f[0] + 0.7152 * f[1] + 0.0722 * f[2]


def _contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# ── palettes ─────────────────────────────────────────────────────────────

def test_every_palette_defines_every_token_as_hex():
    for p in tk.PALETTES.values():
        for t in tk.TOKENS + tuple(tk.ALIASES) + tuple(tk.DERIVED):
            v = p[t]
            assert v.startswith("#") and len(v) == 7, (p.key, t, v)
        assert len(p.series) == 8
        assert p.pair in tk.PALETTES


def test_classic_dark_is_the_original_palette_value_for_value():
    c = tk.PALETTES["classic_dark"]
    original = {"BG": "#1e1e1e", "BG_LIGHT": "#252526",
                "BG_LIGHTER": "#2d2d30", "SURFACE": "#333333",
                "TEXT": "#e0e0e0", "TEXT_DIM": "#a0a0a0",
                "TEXT_DISABLED": "#606060", "ACCENT": "#0078d4",
                "ACCENT_LIGHT": "#3399ff", "ACCENT_DARK": "#005a9e",
                "SUCCESS": "#4caf50", "WARNING": "#ff9800",
                "ERROR": "#f44336", "BORDER": "#3f3f46",
                "SELECTION": "#264f78", "HOVER": "#3a3a3c",
                "GRID": "#2c2c2a", "AXIS": "#4a4a4f"}
    for k, v in original.items():
        assert c[k] == v, k


def test_usafa_palettes_use_official_brand_colors():
    night, day = tk.PALETTES["usafa_night"], tk.PALETTES["usafa_day"]
    assert night["ACCENT_DARK"] == tk.USAFA["academy_blue"]
    assert night["HEADER_BG"] == tk.USAFA["class_royal"]
    assert night["INDICATOR"] == tk.USAFA["class_yellow"]
    assert night["TEXT_DIM"] == tk.USAFA["academy_grey"]
    assert day["ACCENT"] == tk.USAFA["academy_blue"]
    assert day["ERROR"] == tk.USAFA["class_red"]


def test_no_hex_is_ambiguous_within_a_palette():
    """The live remap keys on hex values, so one hex may name only one
    token (and series slot) inside a palette."""
    for p in tk.PALETTES.values():
        seen = {}
        for k, v in p.stored_tokens().items():
            assert v not in seen, (p.key, k, seen.get(v))
            seen[v] = k
        for i, v in enumerate(p.series):
            assert v not in seen, (p.key, f"series {i}", seen.get(v))
            seen[v] = f"series {i}"


@pytest.mark.parametrize("key", list(tk.PALETTES))
def test_text_contrast_meets_wcag(key):
    p = tk.PALETTES[key]
    for fg, bg in (("TEXT", "BG"), ("TEXT", "BG_LIGHT"), ("TEXT", "SURFACE"),
                   ("TEXT", "SELECTION"), ("TEXT", "ROW_DONE"),
                   ("TEXT", "ROW_ACTIVE"), ("TEXT", "ROW_FAILED"),
                   ("HEADER_TEXT", "HEADER_BG"), ("HEADER_DIM", "HEADER_BG"),
                   ("TEXT_DIM", "BG_LIGHT"), ("ACCENT_LIGHT", "BG_LIGHT")):
        assert _contrast(p[fg], p[bg]) >= 4.5, (key, fg, bg)
    # buttons: white on the accent fill, and the tab underline vs page
    assert _contrast(p["ON_ACCENT"], p["ACCENT"]) >= 4.4, key
    assert _contrast(p["INDICATOR"], p["BG"]) >= 3.0, key


# ── remapping ────────────────────────────────────────────────────────────

def test_color_map_moves_tokens_and_series():
    a, b = tk.PALETTES["usafa_night"], tk.PALETTES["usafa_day"]
    m = tk.color_map(a, b)
    assert m[a["BG"]] == b["BG"]
    assert m[a["ACCENT"]] == b["ACCENT"]
    assert m[a.series[1]] == b.series[1]
    css = f"QLabel {{ color: {a['TEXT_DIM']}; background: #123456; }}"
    out = tk.remap_css(css, m)
    assert b["TEXT_DIM"] in out and "#123456" in out


def test_live_switch_updates_module_sinks_and_series_in_place(app):
    series = theme.PALETTE                     # a reference held elsewhere
    tk.manager().apply("usafa_day", persist=False)
    assert theme.BG == tk.PALETTES["usafa_day"]["BG"]
    assert theme.series_color(0) == tk.PALETTES["usafa_day"].series[0]
    assert series is theme.PALETTE and series[0] == theme.series_color(0)
    assert tk.PALETTES["usafa_day"]["BG"] in app.styleSheet()


def test_live_switch_remaps_baked_widget_styles_and_rich_text(app):
    tk.manager().apply("usafa_night", persist=False, force=True)
    w = QWidget()
    w.setStyleSheet(f"background: {theme.BG_LIGHT}; color: {theme.TEXT};")
    lbl = QLabel(f"<span style='color:{theme.TEXT_DIM}'>dim</span>", w)
    tk.manager().apply("classic_light", persist=False)
    day = tk.PALETTES["classic_light"]
    assert day["BG_LIGHT"] in w.styleSheet() and day["TEXT"] in w.styleSheet()
    assert day["TEXT_DIM"] in lbl.text()


def test_adopt_reskins_classic_styled_panels(app):
    """Device panels built from their own (classic) theme copy get the
    active palette when their window is adopted."""
    tk.manager().apply("usafa_night", persist=False, force=True)
    w = QWidget()
    w.setStyleSheet(f"background: {tk.CLASSIC['BG_LIGHT']};")
    tk.manager().adopt(w)
    assert tk.PALETTES["usafa_night"]["BG_LIGHT"] in w.styleSheet()


def test_foreign_device_theme_modules_are_synced(app):
    import types
    mod = types.ModuleType("fake_device.theme")
    mod.BG, mod.TEXT, mod.ACCENT = "#1e1e1e", "#e0e0e0", "#0078d4"
    mod.PALETTE = ["#000000"] * 8
    sys.modules["fake_device.theme"] = mod
    try:
        tk.manager().apply("usafa_day", persist=False)
        day = tk.PALETTES["usafa_day"]
        assert mod.BG == day["BG"] and mod.ACCENT == day["ACCENT"]
        assert mod.PALETTE[0] == day.series[0]
    finally:
        del sys.modules["fake_device.theme"]


def test_pyqtgraph_curves_and_background_follow_the_theme(app):
    import pyqtgraph as pg
    tk.manager().apply("usafa_night", persist=False, force=True)
    pw = pg.PlotWidget()
    curve = pw.plot([0, 1], [0, 1], pen=pg.mkPen(theme.series_color(2)))
    tk.manager().apply("usafa_day", persist=False)
    day = tk.PALETTES["usafa_day"]
    assert pw.backgroundBrush().color().name() == day["PLOT_BG"]
    assert curve.opts["pen"].color().name() == day.series[2]


def test_scale_and_density_reach_the_stylesheet(app):
    tk.manager().apply(scale=1.3, density="compact", persist=False)
    css = app.styleSheet()
    assert "font-size: 13.0pt" in css
    assert app.font().pointSizeF() == pytest.approx(13.0)
    tk.manager().zoom(+0.1)
    assert tk.manager().scale == pytest.approx(1.4)
    tk.manager().apply(scale=99, persist=False)
    assert tk.manager().scale == tk.SCALE_RANGE[1]


def test_toggle_light_dark_uses_the_pair(app):
    tk.manager().apply("usafa_night", persist=False, force=True)
    tk.manager().toggle_light_dark()
    assert tk.current().key == "usafa_day"
    tk.manager().toggle_light_dark()
    assert tk.current().key == "usafa_night"


def test_persistence_round_trip(app, tmp_path):
    from PyQt6.QtCore import QSettings
    store = QSettings(str(tmp_path / "t.ini"), QSettings.Format.IniFormat)
    m = tk.manager()
    try:
        m.init(app, store)
        m.apply("high_contrast", scale=1.2, density="spacious")
        store.sync()
        assert store.value("appearance/theme") == "high_contrast"
        m.apply("usafa_night", persist=False)
        m.init(app, store)
        assert m.palette.key == "high_contrast"
        assert m.scale == pytest.approx(1.2)
        assert m.density_key == "spacious"
        store.setValue("appearance/theme", "dark")       # legacy value
        m.init(app, store)
        assert m.palette.key == "classic_dark"
    finally:
        m._settings = None


# ── view tooling ─────────────────────────────────────────────────────────

def test_fuzzy_score_prefers_contiguous_and_word_starts():
    assert tk.fuzzy_score("", "anything") == 0
    assert tk.fuzzy_score("xyz", "File ▸ Save") is None
    assert tk.fuzzy_score("save", "File ▸ Save Config") > \
        tk.fuzzy_score("sve", "File ▸ Save Config")
    assert tk.fuzzy_score("ms", "File ▸ Measurement Setup") is not None


def test_view_menu_and_command_palette(app):
    win = QMainWindow()
    file_menu = win.menuBar().addMenu("&File")
    hit = []
    act = QAction("&Save Config…", win)
    act.triggered.connect(lambda: hit.append(1))
    file_menu.addAction(act)
    tk.install_view_menu(win)
    paths = [p for p, _a in tk.collect_actions(win)]
    assert "File ▸ Save Config" in paths
    assert any(p.startswith("View ▸ Theme ▸ USAFA Day") for p in paths)
    pal = tk.CommandPalette(win)
    pal.edit.setText("save conf")
    assert pal.list.count() >= 1
    pal._run(pal.list.currentItem())
    app.processEvents()
    assert hit == [1]


def test_appearance_dialog_cancel_reverts(app):
    tk.manager().apply("usafa_night", persist=False, force=True)
    dlg = tk.AppearanceDialog()
    dlg.cards["classic_light"].click()
    assert tk.current().key == "classic_light"
    assert dlg.cards["classic_light"].isChecked()
    dlg.reject()
    assert tk.current().key == "usafa_night"


# ── branding ─────────────────────────────────────────────────────────────

def test_brand_assets_ship_and_render(app):
    for name in ("dfan-aeronautics.png", "usafa-crest.png",
                 "wordmark-horizontal.png", "logomark-without-border.svg"):
        assert (tk.ASSETS / name).is_file(), name
    assert not tk.logo_pixmap(height=32).isNull()
    assert not tk.logo_pixmap("logomark-without-border.svg", 24).isNull()
    assert not tk.app_icon().isNull()
    assert not tk.make_splash_pixmap("Freestream", "1.0").isNull()


def test_themed_icon_repaints_in_active_color(app):
    tpl = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 4">'
           '<rect width="4" height="4" fill="{color}"/></svg>')
    icon = tk.themed_svg_icon(tpl, "TEXT")
    tk.manager().apply("usafa_night", persist=False, force=True)
    c1 = icon.pixmap(8, 8).toImage().pixelColor(4, 4).name()
    tk.manager().apply("usafa_day", persist=False)
    c2 = icon.pixmap(8, 8).toImage().pixelColor(4, 4).name()
    assert c1 == tk.PALETTES["usafa_night"]["TEXT"]
    assert c2 == tk.PALETTES["usafa_day"]["TEXT"]


def test_main_window_has_brand_view_menu_and_theme_chip(app, tmp_path):
    import json
    from freestream.config import FreestreamConfig
    from freestream.manager import DeviceManager
    from freestream.app.main_window import FreestreamMainWindow
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({
        "modes": {"mode1": {"positioner": "pos", "balance": "balance"}},
        "devices": {
            "balance": {"adapter": "freestream._fakes.FakeStreamer",
                        "enabled": True},
            "pos": {"adapter": "freestream._fakes.FakePositioner",
                    "enabled": True}}}), encoding="utf-8")
    mgr = DeviceManager("mode1", sim=True, manifest_path=manifest)
    win = FreestreamMainWindow(
        FreestreamConfig(operator="t", config_name="c",
                         data_root=str(tmp_path)), manager=mgr)
    try:
        menus = [a.text() for a in win.menuBar().actions()]
        assert "&View" in menus and menus[-1] == "&Help"
        assert win.command_bar.objectName() == "commandBar"
        assert win.brand.title.text() == "Freestream"
        assert win.theme_toggle.text().endswith(tk.current().name)
        tk.manager().apply("usafa_day", persist=False)
        assert win.theme_toggle.text().endswith("USAFA Day")
        win.sim_badge.text()                           # still alive
    finally:
        win.close()
