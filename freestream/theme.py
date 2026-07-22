"""Dark theme for Freestream (Streamlined family palette).

Self-contained copy of the ``Streamlined`` palette (VS-Code-style dark:
``#1e1e1e`` background, ``#0078d4`` accent) so this standalone app is visually
consistent with the Streamlined GUI it integrates with.
"""

# â”€â”€ palette (Streamlined DarkTheme) â”€â”€
BG = "#1e1e1e"
BG_LIGHT = "#252526"
BG_LIGHTER = "#2d2d30"
SURFACE = "#333333"

TEXT = "#e0e0e0"
TEXT_DIM = "#a0a0a0"
TEXT_DISABLED = "#606060"

ACCENT = "#0078d4"
ACCENT_LIGHT = "#3399ff"
ACCENT_DARK = "#005a9e"

SUCCESS = "#4caf50"
WARNING = "#ff9800"
ERROR = "#f44336"

BORDER = "#3f3f46"
SELECTION = "#264f78"
HOVER = "#3a3a3c"

# â”€â”€ data-viz series palette (validated categorical set, dark surface) â”€â”€
# Channels get colors by enabled order; a color follows its channel in every
# panel.  All â‰¥3:1 contrast on BG_LIGHT; order is the CVD-safe reference order.
PALETTE = [
    "#3987e5",   # blue    (slot 1 â€” Pdiff in the standard setup)
    "#199e70",   # aqua    (slot 2 â€” Ptot)
    "#c98500",   # yellow  (slot 3 â€” Temp)
    "#008300",   # green
    "#9085e9",   # violet
    "#e66767",   # red
    "#d55181",   # magenta
    "#d95926",   # orange
]


def series_color(index: int) -> str:
    return PALETTE[index % len(PALETTE)]

# Chart chrome (recessive grid/axis ink for pyqtgraph)
PLOT_BG = BG_LIGHT
GRID = "#2c2c2a"
AXIS = "#4a4a4f"


def get_stylesheet() -> str:
    return f"""
    QMainWindow, QWidget#root {{ background-color: {BG}; }}
    QWidget {{ background-color: {BG}; color: {TEXT};
               font-family: "Segoe UI", sans-serif; font-size: 10pt; }}

    QGroupBox {{ background-color: {BG_LIGHT}; border: 1px solid {BORDER};
                 border-radius: 6px; margin-top: 12px; padding-top: 10px;
                 font-weight: bold; }}
    QGroupBox::title {{ subcontrol-origin: margin; subcontrol-position: top left;
                        left: 10px; padding: 0 5px; color: {ACCENT_LIGHT}; }}

    QLabel {{ background: transparent; color: {TEXT}; }}
    QLabel#dim {{ color: {TEXT_DIM}; }}
    QLabel#mono {{ font-family: "Consolas", monospace; color: {SUCCESS}; }}
    QLabel#value {{ font-family: "Consolas", monospace; font-size: 17pt;
                    color: {ACCENT_LIGHT}; }}
    QLabel#unit {{ color: {TEXT_DIM}; }}

    QPushButton {{ background-color: {SURFACE}; border: 1px solid {BORDER};
                   border-radius: 4px; padding: 6px 14px; color: {TEXT};
                   min-height: 22px; }}
    QPushButton:hover {{ background-color: {HOVER}; border-color: {ACCENT}; }}
    QPushButton:pressed {{ background-color: {ACCENT_DARK}; }}
    QPushButton:disabled {{ background-color: {BG_LIGHTER}; color: {TEXT_DISABLED}; }}
    QPushButton:checked {{ background-color: {ACCENT}; border-color: {ACCENT};
                           color: white; font-weight: bold; }}
    QPushButton#primary {{ background-color: {ACCENT}; border: none; color: white;
                           font-weight: bold; }}
    QPushButton#primary:hover {{ background-color: {ACCENT_LIGHT}; }}
    QPushButton#success {{ background-color: {SUCCESS}; border: none; color: white;
                           font-weight: bold; }}
    QPushButton#danger {{ background-color: {ERROR}; border: none; color: white;
                          font-weight: bold; }}
    QPushButton#primary:disabled, QPushButton#success:disabled,
    QPushButton#danger:disabled {{ background-color: {BG_LIGHTER};
                                   color: {TEXT_DISABLED}; }}

    QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
        background-color: {SURFACE}; border: 1px solid {BORDER};
        border-radius: 4px; padding: 5px 8px; color: {TEXT};
        selection-background-color: {ACCENT}; }}
    QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
        border-color: {ACCENT}; }}
    QComboBox QAbstractItemView {{ background-color: {SURFACE};
        border: 1px solid {BORDER}; selection-background-color: {SELECTION};
        color: {TEXT}; }}

    QTableWidget {{ background-color: {BG_LIGHT}; border: 1px solid {BORDER};
                    border-radius: 4px; gridline-color: {BORDER};
                    font-family: "Consolas", monospace; }}
    QTableWidget::item:selected {{ background-color: {SELECTION}; }}
    QHeaderView::section {{ background-color: {BG_LIGHTER}; border: none;
        border-right: 1px solid {BORDER}; border-bottom: 1px solid {BORDER};
        padding: 6px; color: {ACCENT_LIGHT}; font-weight: bold; }}

    QTabWidget::pane {{ background-color: {BG_LIGHT}; border: 1px solid {BORDER};
                        border-radius: 4px; top: -1px; }}
    QTabBar::tab {{ background-color: {BG_LIGHTER}; border: 1px solid {BORDER};
                    border-bottom: none; border-top-left-radius: 4px;
                    border-top-right-radius: 4px; padding: 8px 18px;
                    margin-right: 2px; color: {TEXT_DIM}; }}
    QTabBar::tab:selected {{ background-color: {BG_LIGHT}; color: {TEXT};
                             border-bottom: 2px solid {ACCENT}; }}
    QTabBar::tab:hover:!selected {{ background-color: {HOVER}; color: {TEXT}; }}

    QCheckBox {{ spacing: 8px; color: {TEXT}; }}
    QCheckBox::indicator {{ width: 16px; height: 16px; border: 1px solid {BORDER};
        border-radius: 3px; background-color: {SURFACE}; }}
    QCheckBox::indicator:checked {{ background-color: {ACCENT};
        border-color: {ACCENT}; }}

    QToolBar {{ background-color: {BG}; border: none;
                padding: 5px 8px; spacing: 8px; }}
    QToolBar::separator {{ background-color: {BORDER}; width: 1px;
                           margin: 5px 8px; }}

    QStatusBar {{ background-color: {BG_LIGHT}; border-top: 1px solid {BORDER};
                  color: {TEXT_DIM}; }}
    QMenuBar {{ background-color: {BG_LIGHT}; border-bottom: 1px solid {BORDER}; }}
    QMenuBar::item {{ padding: 4px 10px; }}
    QMenuBar::item:selected {{ background-color: {HOVER}; }}
    QMenu {{ background-color: {SURFACE}; border: 1px solid {BORDER};
             padding: 4px; }}
    QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 3px; }}
    QMenu::item:selected {{ background-color: {SELECTION}; }}
    QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}

    QToolTip {{ background-color: {SURFACE}; color: {TEXT};
                border: 1px solid {ACCENT}; padding: 4px 8px; }}

    QSplitter::handle {{ background-color: {BORDER}; }}
    QSplitter::handle:vertical {{ height: 3px; }}
    QSplitter::handle:horizontal {{ width: 3px; }}
    QSplitter::handle:hover {{ background-color: {ACCENT}; }}

    QScrollBar:vertical {{ background: {BG_LIGHT}; width: 12px; margin: 0; }}
    QScrollBar::handle:vertical {{ background: {SURFACE}; border-radius: 5px;
                                   min-height: 24px; margin: 2px; }}
    QScrollBar::handle:vertical:hover {{ background: {HOVER}; }}
    QScrollBar:horizontal {{ background: {BG_LIGHT}; height: 12px; margin: 0; }}
    QScrollBar::handle:horizontal {{ background: {SURFACE}; border-radius: 5px;
                                     min-width: 24px; margin: 2px; }}
    QScrollBar::handle:horizontal:hover {{ background: {HOVER}; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}

    QDialog {{ background-color: {BG}; }}
    """


def apply_pyqtgraph_theme() -> None:
    """Set pyqtgraph global options to match the dark palette.

    Call once before any plot widget is created.
    """
    import pyqtgraph as pg
    pg.setConfigOption("background", PLOT_BG)
    pg.setConfigOption("foreground", TEXT_DIM)
    pg.setConfigOption("antialias", True)
