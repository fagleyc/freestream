"""Version + about metadata for strainbook_616."""

__version__ = "1.3.0"

APP_NAME = "StrainBook/616 — Balance Bridge DAQ"
AUTHOR = "C. Fagley"
CONTACT = "casey.fagley@afacademy.af.edu"

#: (version, date, summary) — newest first
VERSION_HISTORY = [
    ("1.3.0", "2026-07-23",
     "In-app Help system: bundled HTML documentation and About dialog."),
    ("1.2.0", "2026-07-17",
     "Per-channel plot visibility toggles and envelope-decimated redraw "
     "performance on the live history."),
    ("1.1.0", "2026-07-16",
     "Signed-int16 decode + per-channel scan polarity fix (bridges bipolar, "
     "excitation unipolar with +5 V half-span shift); external excitation "
     "readback via CH8; verified end-to-end with the cabled balance."),
    ("1.0.0", "2026-07-06",
     "Initial DaqX driver + dark PyQt6 GUI; live bring-up: internal WBK16 "
     "bank mapping (front-panel CH n = DaqX channel n+8) established, "
     "internal excitation banks verified then retired in favour of the "
     "rig's external supply."),
]
