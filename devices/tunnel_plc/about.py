"""Version + about metadata for tunnel_plc."""

__version__ = "1.1.0"

APP_NAME = "SSWT Tunnel — Red Lion G315"
AUTHOR = "C. Fagley"
CONTACT = "casey.fagley@afacademy.af.edu"

#: One-paragraph summary shown in the About dialog.
SUMMARY = (
    "Driver and PyQt6 GUI for the sub-sonic wind tunnel itself — fan "
    "speed and start/stop — via the Red Lion G315 HMI at 192.168.1.50 "
    "acting as a Modbus TCP slave (port 502, unit 1). Strictly "
    "separated read-only monitor and ARM-gated control paths: no "
    "write-capable object even exists until the operator arms writes, "
    "and every write is clamped, interlocked and logged."
)

#: (version, date, summary) — newest first
VERSION_HISTORY = [
    ("1.1.0", "2026-07",
     "FC16 writes with automatic FC6 fallback + write-rejection "
     "diagnosis (Crimson read-only / block-direction fix documented); "
     "opt-in bearing-temperature Block1 extension (elements 17–19); "
     "transport-exception hardening in the poll loop."),
    ("1.0.0", "2026-07-07",
     "Initial gateway driver + GUI: read-only TunnelMonitor / "
     "ARM-gated TunnelControl split, momentary fan-button pulses, "
     "rpm_max clamp with refuse-at-0 default. Live-verified low-first "
     "word order and Crimson ×10 fixed-point RPM scaling "
     "(probe_tunnel.py, read-only)."),
]
