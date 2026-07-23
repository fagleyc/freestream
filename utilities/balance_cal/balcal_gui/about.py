"""
About / version metadata for the balance_cal GUI.

Shared metadata template used across the wind-tunnel software ecosystem
(freestream, balance_cal, Streamlined): version, app name, author,
contact, and a compact version history (newest first).
"""

__version__ = "1.1.0"
APP_NAME = "Balance Cal"
AUTHOR = "C. Fagley"
CONTACT = "casey.fagley@afacademy.af.edu"

# (version, iso_date, one_line) — newest first.
VERSION_HISTORY = [
    ("1.1.0", "2026-07-23",
     "Help menu with About dialog and HTML documentation"),
    ("1.0.0", "2026-07-22",
     "Interactive fit diagnostics: outlier repair / exclude-and-refit, "
     "off-diagonal channel scan, field regression from 50lbCalV6"),
    ("0.9.0", "2026-07",
     "Initial Python port of the MATLAB ForceCal app: NI USB-6351 / "
     "StrainBook 616 acquisition, 12-orientation procedure, live cal "
     "plots, .vol 3.1 writer, simulation mode, freestream embedding"),
]

SUMMARY = (
    "Balance Cal acquires bridge voltages from an analog-input DAQ while "
    "dead-weight loads are applied to a force or moment balance, guides the "
    "operator through all twelve loading orientations, fits and diagnoses "
    "the calibration matrix, and writes a Voltage Calibration File 3.1 "
    "(.vol) consumable by the freestream suite's Forces monitor and by "
    "Streamlined. It is the Python port of the MATLAB FB_Cal_GUI/ForceCal "
    "app, runnable standalone (NI USB-6351 or StrainBook/616 backends, "
    "with full simulation support) or embedded in freestream via "
    "Advanced ▸ Balance Calibration."
)
