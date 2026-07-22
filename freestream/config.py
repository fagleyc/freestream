"""Freestream measurement configuration — the SETUP side.

Deliberately separate from the run sheet (test parameters): this file
answers "which devices, what rates, what timeouts, where do files go";
the run sheet answers "what points with what metadata". Two concerns,
two files (spec §5.2).
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Dict, Optional


@dataclass
class FreestreamConfig:
    """All user-tunable Freestream settings (JSON save/load)."""

    #: manifest mode name. Legacy configs carrying "mode1"/"mode2"/
    #: "mode3" are normalized to the current names in :meth:`from_dict`.
    mode: str = "SWT-AC-Internal"
    #: Custom-mode device pick (list of manifest device ids). Only used
    #: when ``mode == "custom"``; empty for the manifest modes. Persisted
    #: so Save/Load round-trips the exact chosen subset.
    custom_devices: list = field(default_factory=list)
    sim: bool = True
    operator: str = ""
    config_name: str = "default"     # folder-per-configuration name
    data_root: str = "runs"          # root folder for HDF5 output

    # ── acquisition defaults (per point; run sheet may override) ────────
    samples: int = 2000              # target samples on the balance
    dwell_s: float = 0.5             # settle dwell before acquiring
    #: ONE suite-wide sample rate, pushed into every streaming driver that
    #: supports it at connect (DaqBook/StrainBook drivers default 200 Hz).
    sample_rate_hz: float = 200.0
    zero_each_point: bool = False    # tare balances before every point

    # ── waits / interlocks ───────────────────────────────────────────────
    move_timeout_s: float = 60.0
    tunnel_timeout_s: float = 120.0
    settle_poll_s: float = 0.1
    refuse_on_blockers: bool = True  # spec §6.2 hard requirement

    # ── Mach targeting (freestream.machloop; tunnel PLC stays RPM) ──────
    #: Tunnel RPM control is OFF by default: the Red Lion currently
    #: REJECTS all Block2 writes (Crimson fix pending), so Freestream is
    #: MONITOR-ONLY until then — mach/rpm points never command the fan;
    #: the sweep waits (operator dialog) for the OPERATOR to bring the
    #: console to the target condition, then records honestly.
    tunnel_control_enabled: bool = False
    #: Verify Mach at each point (operator dialog / settle check). True
    #: (default): current behavior — monitor-only mach/rpm points raise
    #: the operator MachWaitDialog and wait for the settle check. False:
    #: the per-point Mach gate is SKIPPED entirely — no operator dialog,
    #: no mach settle wait; each point records immediately after
    #: positioning (tunnel channels still recorded honestly).
    mach_check_enabled: bool = True
    #: measured Mach must hold within mach_tolerance this long before the
    #: operator-wait dialog auto-proceeds to the point.
    mach_settle_s: float = 2.0
    #: linear Mach→RPM map for the initial command. Conservative default —
    #: MUST be tuned on the rig before LIVE Mach sweeps mean anything.
    rpm_per_mach: float = 1500.0
    #: |measured − target| Mach band for "at target" (LIVE closure).
    mach_tolerance: float = 0.01
    #: max RPM commands per point (initial + corrections) before FAULT.
    mach_max_iterations: int = 3

    # ── calibration POINTERS (recorded in metadata, never applied) ──────
    cal_files: Dict[str, str] = field(default_factory=dict)

    # ── balance reduction (DISPLAY-ONLY live forces; never persisted) ───
    vol_path: str = ""               # balance .vol calibration pointer
    cal_type: str = "Linear"         # Linear | Quadratic | Cubic
    balance_config: str = "Force"    # Force | Moment balance layout
    warn_utilization: float = 0.8    # amber threshold (fraction of rated)
    ref_area: float = 1.0            # S — reference area (coefficient denom)
    ref_chord: float = 1.0           # c — reference chord
    ref_span: float = 1.0            # b — reference span

    # ── run-sheet test info (merged in on Import Run Sheet; §5) ──────────
    #: Header fields from the run-sheet workbook's Test Info tab, recorded
    #: into the HDF5 metadata so a data file knows which test/model it came
    #: from without re-typing. Empty when no run sheet has been imported.
    test_name: str = ""
    model_name: str = ""
    facility: str = ""
    engineer: str = ""
    data_prefix: str = ""            # data file prefix from the run sheet
    objectives: str = ""             # test objectives (free text)

    # ── model reference dimensions (run-sheet Test Info; §5) ─────────────
    #: Reference geometry used DOWNSTREAM (Streamlined) for coefficient
    #: reduction. Sref/cref/bref mirror ref_area/ref_chord/ref_span but are
    #: sourced from the run sheet and carry the moment reference center.
    Sref: float = 0.0                # reference area  (0 = not set)
    cref: float = 0.0                # reference chord (MAC)
    bref: float = 0.0                # reference span
    MRC_x: float = 0.0               # moment ref center X (model axes)
    MRC_y: float = 0.0               # moment ref center Y
    MRC_z: float = 0.0               # moment ref center Z

    # ── output format ───────────────────────────────────────────────────
    #: Per-point file format — ONE primary file per test point:
    #: ``"h5"`` (HDF5, default), ``"mat"`` (MATLAB v5, needs scipy) or
    #: ``"xlsx"`` (Excel workbook, needs openpyxl). HDF5/.mat are readable
    #: by Streamlined for reduction; .xlsx is for spreadsheet review.
    #: Old configs carrying the removed ``write_mat`` flag still load —
    #: ``from_dict`` drops unknown keys, so the legacy flag is ignored and
    #: the format falls back to "h5".
    output_format: str = "h5"

    # ── file naming (spec §5.1) ─────────────────────────────────────────
    #: Template over point_meta / run fields; empty = built-in structured
    #: name. Placeholders: {run}, {alpha}, {beta}, {mach}, {air_state},
    #: {dir}, {config_name}, {operator}, plus any run-sheet column.
    filename_template: str = ""
    tag_hysteresis: bool = True      # append _up/_dn on return-sweep points

    # ── every device's own driver config, snapshotted on Save Config ────
    device_configs: Dict[str, dict] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FreestreamConfig":
        known = {f for f in cls.__dataclass_fields__}      # noqa: E1101
        cfg = cls(**{k: v for k, v in d.items() if k in known})
        # migrate legacy mode names (saved configs / defaults bundles
        # predating the intuitive names) to the current manifest names
        from .manager import LEGACY_MODE_ALIASES       # lazy: keep light
        cfg.mode = LEGACY_MODE_ALIASES.get(cfg.mode, cfg.mode)
        return cfg

    def save(self, path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2),
                              encoding="utf-8")

    @classmethod
    def load(cls, path) -> "FreestreamConfig":
        return cls.from_dict(json.loads(Path(path).read_text(
            encoding="utf-8")))


def defaults_path() -> Path:
    """The startup-defaults file — SEPARATE from named Save/Load Config
    files. "Set as defaults" snapshots the entire current state here
    (measurement settings, sample rate, directories, output format, plus
    every device's own driver config: ranges, resolutions, rates) and the
    app auto-loads it at launch when no explicit --config is given.

    Override with the ``FREESTREAM_DEFAULTS`` env var (tests).
    """
    import os
    env = os.environ.get("FREESTREAM_DEFAULTS")
    return Path(env) if env else Path.home() / ".freestream" / "defaults.json"
