"""Version + about metadata for lswt_sting."""

__version__ = "1.1.0"

APP_NAME = "LSWT Sting Drive"
AUTHOR = "C. Fagley"
CONTACT = "casey.fagley@afacademy.af.edu"

#: One-paragraph summary shown in the About dialog.
SUMMARY = (
    "Driver and PyQt6 GUI for the LSWT model sting: two serial stepper "
    "indexers daisy-chained on one RS-232 port (9600-8N1), unit 1 = "
    "Alpha, unit 2 = Beta. The protocol was recovered from the "
    "deployed C# Tool_LSWT_Sting. Open-loop position (indexer step "
    "counter, operator zeroing), host-side soft limits, latched stall "
    "faults, park-on-disconnect and continuous position checkpointing "
    "— the sting has no brake."
)

#: (version, date, summary) — newest first
VERSION_HISTORY = [
    ("1.1.0", "2026-07-22",
     "Axis direction signs field-verified on the rig (Alpha −1, "
     "Beta +1 — the legacy absolute path carried a dormant Beta sign "
     "bug); serial line-discipline hardening from live COM testing "
     "(partial-line reassembly, echo resync, blind bring-up "
     "commands); COM-port search."),
    ("1.0.0", "2026-07-17",
     "Initial dual-axis driver + GUI: RS-232 indexer protocol "
     "(A/AD/V, D+G moves, PR/PZ, R status) recovered from the "
     "deployed C#; open-loop zeroing with absolute-move lockout, "
     "soft travel limits, stall fault latch, park-on-disconnect, "
     "position checkpoint/restore."),
]
