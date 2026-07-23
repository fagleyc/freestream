"""Version + about metadata for heise."""

__version__ = "1.0.0"

APP_NAME = "Heise PM Indicator"
AUTHOR = "C. Fagley"
CONTACT = "casey.fagley@afacademy.af.edu"

#: One-paragraph summary shown in the About dialog.
SUMMARY = (
    "Driver and PyQt6 GUI for the Heise PM digital indicator — live "
    "pressure and temperature over the RS-232 remote protocol "
    "(per the PM manual, §13 and Appendix A). Polls the '?' query, "
    "remotely selects pressure engineering units (EUNIT), and exposes "
    "ZERO / TARE / damping / battery, with a COM-port search and a "
    "hardware-free simulator."
)

#: (version, date, summary) — newest first
VERSION_HISTORY = [
    ("1.0.0", "2026-07-22",
     "Initial driver + GUI: RS-232 remote protocol per the PM manual "
     "(300–9600 baud 8N1, CR/LF-tolerant line reader), '?' polling "
     "loop with serial watchdog, EUNIT pressure-unit control, "
     "ZERO/TARE/DAMP/BATCK helpers, COM-port search, simulator, "
     "big-number tiles + stacked history plots."),
]
