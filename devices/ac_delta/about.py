"""Version + about metadata for ac_delta."""

__version__ = "1.2.0"

APP_NAME = "ARC Crescent — SSWT Sting Drive"
AUTHOR = "C. Fagley"
CONTACT = "casey.fagley@afacademy.af.edu"

#: (version, date, summary) — newest first
VERSION_HISTORY = [
    ("1.2.0", "2026-07-23",
     "In-app Help system: bundled HTML documentation and About dialog."),
    ("1.1.0", "2026-07-06",
     "Live bring-up: encoder read corrected to wire address 8713 (signed "
     "16-bit), Delta C2000 control word (0x2000) decoded, on-rig two-point "
     "slopes measured (Alpha 294.83 / Beta 202.96 clicks per degree)."),
    ("1.0.0", "2026-07-06",
     "Initial dual Delta C2000 driver + dark PyQt6 GUI: persistent Modbus "
     "TCP, 50 ms host position loop, hold-to-run jog, synchronous dual-axis "
     "moves, soft limits, E-STOP and Modbus watchdog."),
]
