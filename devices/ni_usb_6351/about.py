"""Version + about metadata for ni_usb_6351."""

__version__ = "1.0.0"

APP_NAME = "NI USB-6351 — Analog I/O DAQ"
AUTHOR = "C. Fagley"
CONTACT = "casey.fagley@afacademy.af.edu"

#: One-paragraph summary shown in the About dialog.
SUMMARY = (
    "Driver and PyQt6 GUI for the NI USB-6351 (X series) data "
    "acquisition box wired to the rig's force balance: six bridge "
    "channels on AI0–AI5, excitation readback on AI6, hardware-timed "
    "continuous sampling with PFI/APFI start triggers, two analog "
    "outputs (static levels or regenerated waveforms), and balance "
    ".vol calibration to body-frame forces (Fx, Fy, Fz, Mx, My, Mz)."
)

#: (version, date, summary) — newest first
VERSION_HISTORY = [
    ("1.0.0", "2026-07-17",
     "Initial driver + GUI: NI-DAQmx hardware-timed AI with "
     "digital/analog start triggers, AO static levels + regenerated "
     "waveforms, bridge tare, balance .vol calibration to body-frame "
     "forces with utilization warnings, Force/Moment bridge renaming, "
     "full simulation mode (no NI-DAQmx install required)."),
]
