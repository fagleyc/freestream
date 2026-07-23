"""Version + about metadata for ate_balance."""

__version__ = "1.1.0"

APP_NAME = "ATE External Balance — TMS Client"
AUTHOR = "C. Fagley"
CONTACT = "casey.fagley@afacademy.af.edu"

#: (version, date, summary) — newest first
VERSION_HISTORY = [
    ("1.1.0", "2026-07-23",
     "In-app Help system: bundled HTML documentation and About dialog."),
    ("1.0.0", "2026-07-06",
     "Initial TMS client + dark PyQt6 GUI (wire protocol per "
     "AID-010-10015-1 §6, FakeOGI emulator, dwell averaging, motion/lock "
     "control); verified against the live OGI at 192.168.1.60: 300 Hz "
     "29-byte LOADS stream (USAFA build, no sync word), control link, "
     "position/lock/filter queries."),
]
