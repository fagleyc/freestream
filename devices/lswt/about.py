"""Version + about metadata for lswt."""

__version__ = "1.0.0"

APP_NAME = "LSWT Fan Control — ABB ACS530"
AUTHOR = "C. Fagley"
CONTACT = "casey.fagley@afacademy.af.edu"

#: One-paragraph summary shown in the About dialog.
SUMMARY = (
    "Driver and PyQt6 GUI for the North & South Low-Speed Wind Tunnel "
    "fans, each running on an ABB ACS530 VFD over Modbus TCP (unit 1). "
    "Protocol and the 61-point measured Hz→velocity calibration were "
    "extracted from the deployed C# tool "
    "(Tool_LSWT_Flow_Velocity, HwControllerVelocityLSWT_ACB530.cs). "
    "Host-side setpoint ramp, ARM-gated fan control, and an "
    "always-live E-STOP."
)

#: (version, date, summary) — newest first
VERSION_HISTORY = [
    ("1.0.0", "2026-07-22",
     "Initial ABB ACS530 driver + GUI: START/STOP control words and "
     "negative-reference convention ported from the deployed C#; "
     "61-point measured velocity calibration (0–60 Hz → 0–105.6851 "
     "ft/s); host-side ramp replacing the C#'s slam-to-zero "
     "protection; ARM gating, read-passive connect, per-tunnel "
     "defaults."),
]
