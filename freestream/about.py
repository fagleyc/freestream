"""
About / version metadata for the Freestream suite.

Shared metadata template used across the wind-tunnel software ecosystem
(freestream, balance_cal, Streamlined, device apps): version, app name,
author, contact, and a compact version history (newest first).

History note: the freestream git repository was initialized 2026-07-22,
after most of the suite was already built, so the earlier milestones
below are distilled from in-code milestone evidence (mode renames,
sweep-grammar unification, run-book import, ATE truth-naming, ...)
rather than from commit dates.
"""

__version__ = "2.1.0"
APP_NAME = "Freestream"
AUTHOR = "C. Fagley"
CONTACT = "casey.fagley@afacademy.af.edu"

# (version, iso_date, one_line) — newest first.
VERSION_HISTORY = [
    ("2.1.0", "2026-07-23",
     "LSWT mode (LSWT-LSWTSting-NI: sting + NI DAQ + Heise + ABB fan "
     "drive), embedded device panels, Help/Documentation system"),
    ("2.0.0", "2026-07-22",
     "ATE truth-naming: external balance recorded with real channel "
     "names (legacy alias fallback for old files); device-owned "
     ".vol/cal config; SIM-defaults force_sim guard"),
    ("1.5.0", "2026-07",
     "Pause/Resume at point boundaries; per-point Mach-verify toggle "
     "with monitor-only operator wait (tunnel writes off by default)"),
    ("1.4.0", "2026-07",
     "Output format selection: HDF5 / MATLAB .mat / .xlsx review "
     "workbooks, structured filenames with hysteresis leg tags"),
    ("1.3.0", "2026-07",
     "Run-book workbook import: 5-sheet template with Test Info, Run "
     "Matrix, Model Configs and Named Arrays"),
    ("1.2.0", "2026-07",
     "Unified sweep grammar: start:delta:end ranges, R return sweeps, "
     "@named rows, csv: columns (one canonical parser)"),
    ("1.1.0", "2026-07-08",
     "Intuitive mode names (SWT-AC-Internal / SWT-External / "
     "SWT-Traverse) with legacy mode1/2/3 aliases; custom mode; "
     "adapter consolidation"),
    ("1.0.0", "2026-06",
     "Initial suite: HAL capability roles, per-device adapters, sweep "
     "engine with refuse-to-record interlocks, HDF5 recorder, tunnel "
     "dashboard, Forces page, Streamlined interop"),
]

SUMMARY = (
    "Freestream is the tunnel-side control suite for the wind-tunnel "
    "software ecosystem: it wires the facility's devices (attitude "
    "positioners, internal/external balances, tunnel-condition DAQs, and "
    "fan drives) into capability roles through a hardware abstraction "
    "layer, runs automated sweeps from a planner or run-book workbook, "
    "streams live monitors (tunnel dashboard, balance, position, forces "
    "with peak-hold utilization bars), and records raw, self-describing "
    "per-point run files (HDF5/.mat/.xlsx) that Streamlined reduces. "
    "Calibration is never applied at capture — .vol files are recorded as "
    "pointers only."
)
