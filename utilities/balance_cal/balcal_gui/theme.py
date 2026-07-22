"""House dark theme, re-exported from the ni_usb_6351 driver package so
the calibration app is visually identical to the rest of the suite."""

from .daq import _ensure_devices_path

_ensure_devices_path()

from ni_usb_6351.theme import *          # noqa: E402,F401,F403
from ni_usb_6351 import theme as _t      # noqa: E402

get_stylesheet = _t.get_stylesheet
apply_pyqtgraph_theme = _t.apply_pyqtgraph_theme
series_color = _t.series_color
