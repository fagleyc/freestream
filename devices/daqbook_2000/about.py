"""Version + about metadata for daqbook_2000."""

__version__ = "1.1.0"

APP_NAME = "DaqBook/2000 — Tunnel Conditions DAQ"
AUTHOR = "C. Fagley"
CONTACT = "casey.fagley@afacademy.af.edu"

#: (version, date, summary) — newest first
VERSION_HISTORY = [
    ("1.1.0", "2026-07-23",
     "In-app Help system: bundled HTML documentation and About dialog."),
    ("1.0.0", "2026-07-06",
     "Initial DaqX driver + dark PyQt6 GUI, live-verified on the rig "
     "DaqBook/2005: 19,917 scans / 20 s at the 1 kHz ADC clock with zero "
     "overruns, [690] Ptot slope validated end-to-end; single-ended "
     "bipolar-only range constraint (DaqX error 134) enforced by the "
     "range picker; DaqbookAuxSource serves tunnel q to other consumers."),
]
