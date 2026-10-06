"""Version + about metadata for lswt_traverse."""

__version__ = "1.0.0"

APP_NAME = "South LSWT Traverse — SmartStep23"
AUTHOR = "C. Fagley"
CONTACT = "casey.fagley@afacademy.af.edu"

#: One-paragraph summary shown in the About dialog.
SUMMARY = (
    "Standalone driver and PyQt6 GUI for the South LSWT 3-axis probe "
    "traverse: three IDC SmartStep23 microstepping SmartDrives on one "
    "RS-232C daisy chain (9600 8N1, unit 1 = Z vertical, 2 = Y lateral, "
    "3 = X axial). The drives position themselves (AC/VE/DA…GO), so the "
    "host only transacts and supervises. Referencing is deliberately "
    "minimal: jog to the reference spot, Set home (SP), and host-side "
    "soft travel limits gate everything after — no homing routine."
)

#: (version, date, summary) — newest first
VERSION_HISTORY = [
    ("1.0.0", "2026-09-01",
     "Initial driver: IDeal serial protocol with daisy-chain echo "
     "handling, per-axis absolute moves and hold-to-jog, set-home "
     "referencing with host-side soft limits, monitor thread with "
     "drive-fault surfacing, byte-level chain emulator, PyQt6 GUI."),
]
