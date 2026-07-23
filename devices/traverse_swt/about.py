"""Version + about metadata for traverse_swt."""

__version__ = "1.3.0"

APP_NAME = "SSWT Traverse — WAGO 750"
AUTHOR = "C. Fagley"
CONTACT = "casey.fagley@afacademy.af.edu"

#: One-paragraph summary shown in the About dialog.
SUMMARY = (
    "Standalone driver and PyQt6 GUI for the sub-sonic wind tunnel's "
    "3-axis probe traverse: a WAGO 750 controller at 192.168.1.21:502 "
    "(Modbus TCP) driving three 750-673 stepper modules — X axial, "
    "Y lateral, Z vertical. Host-side bang-bang positioning with "
    "calibrated soft limits, limit-switch reaction, homing, and a "
    "1,000,000-count rollover unwrap into absolute position."
)

#: (version, date, summary) — newest first
VERSION_HISTORY = [
    ("1.3.0", "2026-07-22",
     "Bit-level homing seek direction (home_jog_fwd) pinned "
     "independently of the position bookkeeping; Z sign convention "
     "settled on the rig; X limit input disabled per the rig."),
    ("1.2.0", "2026-07-21",
     "Host-side homing (seek → backoff → datum) and runtime limit "
     "reaction restored; limit-switch polarity rig-verified ACTIVE-LOW "
     "(bit clears when a switch engages)."),
    ("1.1.0", "2026-07",
     "Clean 1,000,000-count rollover unwrap into a continuous absolute "
     "position with a counter-jump guard; retired the 24-bit "
     "counter-limit / MC3_SetPosition re-reference machinery."),
    ("1.0.0", "2026-07-07",
     "Initial driver + GUI reverse-engineered from the CoDeSys PLC "
     "source (LVW_V3_2021.pro) and the deployed C# tool: persistent "
     "Modbus connection, one block read per tick, bang-bang move_to "
     "with wrong-way trip, stall abort, direction-change dwell, "
     "two-point calibration."),
]
