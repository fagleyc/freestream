"""HDF5 recorder — raw, Streamlined-consumable per-test-point files (§5).

One file per test point, folder-per-configuration, generic run numbering
``run_0001 … run_N``. The layout mirrors the TDMS groups Streamlined
already reduces (§5.3)::

    <root>/<config_name>/run_0007_alpha_-2.0_beta_0.0_mach_0.30.h5
      ├─ attrs: run_number, timestamp, mode, config_name, operator,
      │         air_state, + EVERY run-sheet/point_meta key verbatim
      ├─ /StrainBook_0/N1 …    each dset attrs: wf_increment, wf_samples,
      │                        wf_start_time, unit
      ├─ /DaqBook2005/Pdiff …
      ├─ /Positioner/Alpha …
      ├─ /Time/Time
      ├─ /meta/devices/<id>    per-device attrs (model, sim, cal-file POINTER…)
      └─ /meta/config          measurement-config snapshot (json blob)

Hard rules honoured here:

* **Raw data verbatim** — no calibration is ever applied at capture time.
* **Calibration POINTERS only** — cal-file paths live as string attrs in
  ``/meta/devices``; reduction happens later in Streamlined.

Selectable output format (``output_format="h5" | "mat" | "xlsx"``)
------------------------------------------------------------------
The recorder writes ONE primary file per test point in the format chosen
at construction; the ``run_NNNN_alpha_.._mach_..`` basename is identical
across formats, only the extension differs. ``write_point`` returns the
primary file's Path.

* ``"h5"`` (default) — the HDF5 layout above.
* ``"mat"`` — a MATLAB v5 file (:func:`scipy.io.savemat`), written
  directly from the in-memory blocks (no intermediate .h5) and mirroring
  the HDF5 schema (readable by Streamlined's ``read_mat_file``).
* ``"xlsx"`` — an Excel workbook (:mod:`freestream.xlsx_writer`, needs
  openpyxl) for spreadsheet review: one Data sheet per group, plus
  Meta / Channels / Devices / Config sheets.

MATLAB layout (``output_format="mat"``)
---------------------------------------
The ``.mat`` mirrors the HDF5 schema:

* one top-level struct per group — e.g. ``StrainBook_0.N1`` (double column
  vectors), ``Positioner.Alpha``, ``Time.Time`` — group/channel names
  sanitized to valid MATLAB identifiers (see below);
* ``meta.run.<key>``          — every root attr (run params) verbatim;
* ``meta.channels.<G>.<C>``   — struct with ``wf_increment``,
  ``wf_samples``, ``wf_start_time``, ``unit`` per channel;
* ``meta.devices.<name>``     — per-device attrs (cal_file stays a POINTER);
* ``meta.config_json``        — the measurement-config snapshot as a JSON
  string (same blob as ``/meta/config``);
* ``meta.name_map``           — the sanitization mapping, substructs
  ``groups`` / ``channels.<G>`` / ``run`` / ``devices``, each field being
  the **sanitized** name whose value is the **original** name.

Sanitization (:func:`_matlab_name`): every char outside ``[A-Za-z0-9_]``
becomes ``_``; names not starting with a letter get an ``x`` prefix;
names are truncated to MATLAB's 63-char limit and de-duplicated with
``_2``, ``_3`` … suffixes. While ``output_format == "mat"`` the path of
the last ``.mat`` written is also exposed as
:attr:`Hdf5Recorder.last_mat_path` (None for the other formats).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Union

import h5py
import numpy as np

__all__ = ["Hdf5Recorder", "read_point"]

# keys of point_meta that participate in the filename, in this order
# (aero attitude axes first, then the Mode-3 traverse position axes)
_FILENAME_KEYS = ("alpha", "beta", "mach", "x", "y", "z")
# per-key filename formatting overrides (default is _fmt_value)
_KEY_FMT = {"mach": lambda v: f"{float(v):.2f}"}
_RUN_RE = re.compile(r"^run_(\d+)", re.IGNORECASE)
# characters illegal (or unpleasant) in Windows/posix filenames
_BAD_FS_CHARS = re.compile(r'[<>:"/\\|?*\s]+')
# characters illegal in MATLAB identifiers (struct/field/variable names)
_BAD_MAT_CHARS = re.compile(r"[^0-9A-Za-z_]")
_MAT_NAME_MAX = 63          # MATLAB namelengthmax


# ── small helpers ────────────────────────────────────────────────────────
class _DefaultDict(dict):
    """format_map backing that yields '' for any missing placeholder."""

    def __missing__(self, key):                        # noqa: D401
        return ""


def _fmt_value(v: Any) -> str:
    """Filename-friendly rendering: floats keep a decimal (alpha_-2.0)."""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float):
        s = f"{v:g}"
        if all(c not in s for c in (".", "e", "n", "i")):  # nan/inf guards
            s += ".0"
        return s
    return _BAD_FS_CHARS.sub("-", str(v))


def _iso_start(point_meta: Mapping[str, Any]) -> str:
    """wf_start_time: point_meta['t_start'] (datetime | epoch | str) or now."""
    t = point_meta.get("t_start")
    if t is None:
        return datetime.now().isoformat()
    if isinstance(t, datetime):
        return t.isoformat()
    if isinstance(t, (int, float)):
        return datetime.fromtimestamp(float(t)).isoformat()
    return str(t)


def _set_attr(attrs: h5py.AttributeManager, key: str, value: Any) -> None:
    """Write one attribute; str/num/bool verbatim, lists as arrays,
    None skipped, datetimes/Paths/dicts stringified."""
    if value is None:
        return
    if isinstance(value, datetime):
        value = value.isoformat()
    elif isinstance(value, Path):
        value = str(value)
    elif isinstance(value, dict):
        value = json.dumps(value)
    if isinstance(value, (list, tuple, np.ndarray)):
        arr = np.asarray(value)
        if arr.dtype.kind in ("U", "S", "O"):
            arr = np.array([str(v) for v in value],
                           dtype=h5py.string_dtype("utf-8"))
        attrs[key] = arr
        return
    attrs[key] = value


def _from_attr(value: Any) -> Any:
    """Inverse of _set_attr for read_point: bytes→str, numpy→python."""
    if isinstance(value, bytes):
        return value.decode("utf-8")
    if isinstance(value, np.ndarray):
        if value.dtype.kind in ("S", "O"):
            return [v.decode("utf-8") if isinstance(v, bytes) else str(v)
                    for v in value]
        return value
    if isinstance(value, np.generic):
        return value.item()
    return value


def _matlab_name(name: Any, used: Optional[set] = None) -> str:
    """Sanitize *name* into a valid, unique MATLAB identifier.

    Chars outside ``[A-Za-z0-9_]`` → ``_``; an ``x`` prefix is added when
    the name doesn't start with a letter; the result is truncated to 63
    chars (MATLAB ``namelengthmax``). When *used* is given, collisions are
    resolved with ``_2``, ``_3`` … suffixes and the result is added to it.
    """
    s = _BAD_MAT_CHARS.sub("_", str(name))
    if not s or not s[0].isalpha():
        s = "x" + s
    s = s[:_MAT_NAME_MAX]
    if used is not None:
        base, i = s, 2
        while s in used:
            suffix = f"_{i}"
            s = base[:_MAT_NAME_MAX - len(suffix)] + suffix
            i += 1
        used.add(s)
    return s


def _mat_value(value: Any) -> Any:
    """savemat-friendly rendering of one attr value (mirrors _set_attr):
    str/num/bool verbatim, None → None (caller skips), datetime/Path
    stringified, dict → JSON, string lists → object arrays (cell arrays)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return json.dumps(value)
    if isinstance(value, (list, tuple, np.ndarray)):
        arr = np.asarray(value)
        if arr.dtype.kind in ("U", "S", "O"):
            return np.array([str(v) for v in value], dtype=object)
        return arr
    return value


UnitsArg = Union[
    Mapping[str, Any],          # {"N1": "V"}  or  {"StrainBook_0": {"N1": "V"}}
    Iterable[Any],              # iterable of ChannelSpec-likes (.name/.unit/.group)
    None,
]


def _normalize_units(channel_units: UnitsArg) -> Dict[tuple, str]:
    """→ {(group|None, channel): unit}. Accepts a flat {ch: unit} dict, a
    nested {group: {ch: unit}} dict, or an iterable of ChannelSpec."""
    units: Dict[tuple, str] = {}
    if channel_units is None:
        return units
    if isinstance(channel_units, Mapping):
        for key, val in channel_units.items():
            if isinstance(val, Mapping):
                for ch, unit in val.items():
                    units[(key, ch)] = str(unit)
            else:
                units[(None, key)] = str(val)
        return units
    for spec in channel_units:  # ChannelSpec from freestream.hal
        units[(getattr(spec, "group", None), spec.name)] = str(spec.unit)
    return units


# ── recorder ─────────────────────────────────────────────────────────────
class Hdf5Recorder:
    """Writes one raw per-point file under ``root/config_name/``.

    The class name is historical (kept for API stability): the HDF5
    layout remains the reference schema, but ``output_format`` selects
    the primary on-disk format — ``"h5"`` (default), ``"mat"`` or
    ``"xlsx"``. Folder-per-configuration with generic run numbering;
    never model-specific filenames (tunnel-team requirement, §5.1).
    """

    #: supported primary output formats → file extension
    EXTENSIONS = {"h5": ".h5", "mat": ".mat", "xlsx": ".xlsx"}

    def __init__(self, root_dir: Union[str, Path],
                 config_name: str = "default",
                 filename_template: str = "",
                 output_format: str = "h5"):
        self.root_dir = Path(root_dir)
        self.config_name = config_name
        #: optional str.format template over run/point fields; empty =
        #: built-in structured name. Placeholders: {run}, {alpha}, {beta},
        #: {mach}, {air_state}, {dir}, {config_name} + any point_meta key.
        self.filename_template = filename_template or ""
        #: primary per-point file format: "h5" | "mat" | "xlsx".
        fmt = str(output_format or "h5").lower().lstrip(".")
        if fmt not in self.EXTENSIONS:
            raise ValueError(
                f"unknown output_format {output_format!r}; expected one "
                f"of {sorted(self.EXTENSIONS)}")
        self.output_format = fmt
        #: Path of the ``.mat`` written by the most recent write_point
        #: (None until then, and always None unless output_format="mat").
        self.last_mat_path: Optional[Path] = None
        if fmt == "mat":
            try:                                   # fail fast, not per point
                import scipy.io                    # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "output_format='mat' requires scipy (scipy.io.savemat)."
                    " Install with: pip install scipy") from exc
        elif fmt == "xlsx":
            try:                                   # fail fast, not per point
                import openpyxl                    # noqa: F401
            except ImportError as exc:
                raise ImportError(
                    "output_format='xlsx' requires openpyxl. "
                    "Install with: pip install openpyxl") from exc
        self.config_dir = self.root_dir / config_name
        self.config_dir.mkdir(parents=True, exist_ok=True)

    @property
    def extension(self) -> str:
        """File extension of the selected format (``.h5``/``.mat``/…)."""
        return self.EXTENSIONS[self.output_format]

    # ── run numbering ────────────────────────────────────────────────────
    def next_run_number(self) -> int:
        """1 + highest ``run_NNNN`` already present in the config folder.

        Scans EVERY known output extension so switching formats mid-test
        never re-uses a run number.
        """
        highest = 0
        exts = set(self.EXTENSIONS.values())
        for path in self.config_dir.glob("run_*"):
            if path.suffix.lower() not in exts:
                continue
            m = _RUN_RE.match(path.name)
            if m:
                highest = max(highest, int(m.group(1)))
        return highest + 1

    # ── filename ─────────────────────────────────────────────────────────
    def filename_for(self, run_number: int, point_meta: Mapping[str, Any],
                     air_state: str = "AirOn") -> str:
        """Per-point file name.

        With no template: ``run_{NNNN:04d}[_alpha_{a}][_beta_{b}]
        [_mach_{m:.2f}][_{up|dn}]<ext>`` (extension per the selected
        output format) — only the point_meta keys
        actually present appear, and the hysteresis direction token
        (``sweep_dir``) is inserted for return-sweep legs so the two α̇
        signs never collide. The air state is NO LONGER a filename token
        (a ``mach_0.00`` point already marks an air-off/tare condition);
        it is still recorded verbatim as the ``air_state`` root attr.
        With a ``filename_template`` set, the name is ``str.format``-ed
        over the run/point fields (``{air_state}`` remains available)
        instead.
        """
        direction = str(point_meta.get("sweep_dir", "") or "")
        if self.filename_template:
            return self._templated_name(run_number, point_meta, air_state,
                                        direction)
        parts = [f"run_{run_number:04d}"]
        for key in _FILENAME_KEYS:
            if key in point_meta and point_meta[key] is not None:
                fmt = _KEY_FMT.get(key, _fmt_value)
                parts.append(f"{key}_{fmt(point_meta[key])}")
        if direction:
            parts.append(direction)
        # air_state is intentionally NOT a filename token — it lives as a
        # root attr; a mach_0.00 point already encodes an air-off/tare.
        return "_".join(parts) + self.extension

    def _templated_name(self, run_number: int, point_meta: Mapping[str, Any],
                        air_state: str, direction: str) -> str:
        """Render ``filename_template`` — missing fields become ''."""
        fields: Dict[str, Any] = {
            "run": f"{run_number:04d}",
            "run_number": run_number,
            "air_state": air_state,
            "dir": direction,
            "config_name": self.config_name,
        }
        for key, val in point_meta.items():
            fields[key] = _fmt_value(val) if isinstance(val, float) else val
        try:
            name = self.filename_template.format_map(_DefaultDict(fields))
        except Exception:                              # noqa: BLE001
            name = f"run_{run_number:04d}_{air_state}"
        name = _BAD_FS_CHARS.sub("-", name).strip("_-")
        return (name or f"run_{run_number:04d}") + self.extension

    # ── write ────────────────────────────────────────────────────────────
    def write_point(
        self,
        point_meta: Dict[str, Any],
        blocks: Dict[str, Dict[str, np.ndarray]],
        rates: Dict[str, float],
        run_number: Optional[int] = None,
        air_state: str = "AirOn",
        extra_attrs: Optional[Dict[str, Any]] = None,
        device_meta: Optional[List[Dict[str, Any]]] = None,
        config_snapshot: Optional[Dict[str, Any]] = None,
        channel_units: UnitsArg = None,
        time_array: Optional[np.ndarray] = None,
    ) -> Path:
        """Write one test point and return the written primary file path
        (extension per the selected ``output_format``).

        Parameters
        ----------
        point_meta : run-sheet row / point parameters. Every key lands
            verbatim in the root attrs; ``alpha``/``beta``/``mach`` (when
            present) also shape the filename; ``t_start`` (datetime, epoch
            or ISO str) sets each dataset's ``wf_start_time``.
        blocks : {hdf5 group name: {channel: 1-D raw array}} — written
            verbatim as float64, NO calibration applied.
        rates : {group name: sample rate Hz} → per-dataset ``wf_increment``.
        channel_units : per-channel units — a flat ``{ch: unit}`` dict, a
            nested ``{group: {ch: unit}}`` dict, or an iterable of
            :class:`freestream.hal.ChannelSpec`.
        time_array : explicit /Time/Time seconds axis; default is
            ``arange(n)/rate`` for the longest group.
        device_meta : list of per-device dicts (model, sim, firmware,
            cal_file pointer…) → subgroups of ``/meta/devices``.
        config_snapshot : measurement config → json blob at /meta/config.
        """
        if run_number is None:
            run_number = self.next_run_number()
        extra_attrs = extra_attrs or {}
        units = _normalize_units(channel_units)
        start_iso = _iso_start(point_meta)
        path = self.config_dir / self.filename_for(run_number, point_meta,
                                                   air_state)
        # synthesized /Time/Time axis (None when the caller supplies a
        # "Time" block) — computed once, shared by every format writer
        time_axis = (None if "Time" in blocks
                     else self._time_axis(blocks, rates, time_array))

        # ── root attrs: fixed set first, then EVERY meta key verbatim ───
        root: Dict[str, Any] = {
            "run_number": int(run_number),
            "timestamp": datetime.now().isoformat(),
            "mode": "",
            "config_name": self.config_name,
            "operator": "",
            "air_state": air_state,
        }
        root.update(point_meta)
        root.update(extra_attrs)

        # ── format dispatch — every writer works from the in-memory
        #    blocks directly (no intermediate .h5 for mat/xlsx) ──────────
        self.last_mat_path = None
        if self.output_format == "mat":
            self._write_mat(path, root, blocks, rates, units, start_iso,
                            device_meta, config_snapshot, time_axis)
            self.last_mat_path = path
        elif self.output_format == "xlsx":
            from .xlsx_writer import write_xlsx
            write_xlsx(path, root, blocks, rates, units, start_iso,
                       device_meta, config_snapshot, time_axis)
        else:
            self._write_h5(path, root, blocks, rates, units, start_iso,
                           device_meta, config_snapshot, time_axis)
        return path

    # ── HDF5 writer (the reference schema) ───────────────────────────────
    @staticmethod
    def _write_h5(
        path: Path,
        root_attrs: Mapping[str, Any],
        blocks: Dict[str, Dict[str, np.ndarray]],
        rates: Dict[str, float],
        units: Dict[tuple, str],
        start_iso: str,
        device_meta: Optional[List[Dict[str, Any]]],
        config_snapshot: Optional[Dict[str, Any]],
        time_axis: Optional[np.ndarray],
    ) -> Path:
        """Write the ``.h5`` (§5.3 layout) from the assembled point."""
        with h5py.File(path, "w") as f:
            for key, val in root_attrs.items():
                _set_attr(f.attrs, key, val)

            # ── data groups (raw, verbatim) ───────────────────────────────
            for group_name, channels in blocks.items():
                grp = f.create_group(group_name)
                rate = float(rates.get(group_name, 0.0))
                incr = (1.0 / rate) if rate > 0 else 0.0
                for ch_name, data in channels.items():
                    arr = np.asarray(data, dtype=np.float64).ravel()
                    dset = grp.create_dataset(ch_name, data=arr)
                    dset.attrs["wf_increment"] = incr
                    dset.attrs["wf_samples"] = int(arr.size)
                    dset.attrs["wf_start_time"] = start_iso
                    dset.attrs["unit"] = (units.get((group_name, ch_name))
                                          or units.get((None, ch_name))
                                          or "")

            # ── /Time/Time ────────────────────────────────────────────────
            if time_axis is not None:
                t = np.asarray(time_axis, dtype=np.float64).ravel()
                tg = f.create_group("Time")
                dset = tg.create_dataset("Time", data=t)
                incr = (float(t[1] - t[0]) if len(t) > 1 else 0.0)
                dset.attrs["wf_increment"] = incr
                dset.attrs["wf_samples"] = int(len(t))
                dset.attrs["wf_start_time"] = start_iso
                dset.attrs["unit"] = "s"

            # ── /meta ─────────────────────────────────────────────────────
            meta = f.create_group("meta")
            dev_grp = meta.create_group("devices")
            for i, dev in enumerate(device_meta or []):
                name = str(dev.get("id") or dev.get("name")
                           or dev.get("model") or f"device_{i}")
                sub = dev_grp.create_group(name)
                for key, val in dev.items():
                    _set_attr(sub.attrs, key, val)   # cal_file stays a POINTER
            meta.create_dataset(
                "config",
                data=json.dumps(config_snapshot
                                if config_snapshot is not None else {}))
        return path

    # ── MATLAB writer ────────────────────────────────────────────────────
    @staticmethod
    def _write_mat(
        mat_path: Path,
        root_attrs: Mapping[str, Any],
        blocks: Dict[str, Dict[str, np.ndarray]],
        rates: Dict[str, float],
        units: Dict[tuple, str],
        start_iso: str,
        device_meta: Optional[List[Dict[str, Any]]],
        config_snapshot: Optional[Dict[str, Any]],
        time_axis: Optional[np.ndarray],
    ) -> Path:
        """Write the ``.mat`` mirroring the HDF5 schema, directly from
        the in-memory blocks (no intermediate .h5).

        Layout (see module docstring): one top-level struct per group with
        channel arrays as double column vectors, plus a ``meta`` struct
        holding ``run`` (root attrs), ``channels`` (per-channel waveform
        attrs), ``devices`` (cal POINTERS), ``config_json`` and the
        ``name_map`` sanitized→original mapping.
        """
        from scipy.io import savemat

        mat: Dict[str, Any] = {}
        chan_meta: Dict[str, Any] = {}
        name_map: Dict[str, Any] = {"groups": {}, "channels": {},
                                    "run": {}, "devices": {}}
        used_groups = {"meta"}                     # never shadow the meta struct

        def _add_group(group_name: str,
                       channels: Mapping[str, Any],
                       incr: float, unit_of) -> None:
            g_san = _matlab_name(group_name, used_groups)
            name_map["groups"][g_san] = str(group_name)
            g_data: Dict[str, Any] = {}
            g_meta: Dict[str, Any] = {}
            g_names: Dict[str, str] = {}
            used_ch: set = set()
            for ch_name, data in channels.items():
                c_san = _matlab_name(ch_name, used_ch)
                g_names[c_san] = str(ch_name)
                arr = np.asarray(data, dtype=np.float64).ravel()
                g_data[c_san] = arr
                g_meta[c_san] = {
                    "wf_increment": incr,
                    "wf_samples": int(arr.size),
                    "wf_start_time": start_iso,
                    "unit": unit_of(ch_name),
                }
            mat[g_san] = g_data
            chan_meta[g_san] = g_meta
            name_map["channels"][g_san] = g_names

        for group_name, channels in blocks.items():
            rate = float(rates.get(group_name, 0.0))
            incr = (1.0 / rate) if rate > 0 else 0.0
            _add_group(group_name, channels, incr,
                       lambda ch, g=group_name: (units.get((g, ch))
                                                 or units.get((None, ch))
                                                 or ""))

        # synthesized Time group — mirrors /Time/Time exactly
        if time_axis is not None:
            t = np.asarray(time_axis, dtype=np.float64).ravel()
            incr = (float(t[1] - t[0]) if len(t) > 1 else 0.0)
            _add_group("Time", {"Time": t}, incr, lambda ch: "s")

        # meta.run — every root attr verbatim (run params incl. custom
        # run-sheet columns); None skipped just like the HDF5 attrs
        run_struct: Dict[str, Any] = {}
        used_run: set = set()
        for key, val in root_attrs.items():
            v = _mat_value(val)
            if v is None:
                continue
            k_san = _matlab_name(key, used_run)
            name_map["run"][k_san] = str(key)
            run_struct[k_san] = v

        # meta.devices — attrs verbatim; cal_file stays a POINTER string
        devices: Dict[str, Any] = {}
        used_dev: set = set()
        for i, dev in enumerate(device_meta or []):
            name = str(dev.get("id") or dev.get("name")
                       or dev.get("model") or f"device_{i}")
            d_san = _matlab_name(name, used_dev)
            name_map["devices"][d_san] = name
            d_struct: Dict[str, Any] = {}
            used_keys: set = set()
            for key, val in dev.items():
                v = _mat_value(val)
                if v is None:
                    continue
                d_struct[_matlab_name(key, used_keys)] = v
            devices[d_san] = d_struct

        mat["meta"] = {
            "run": run_struct,
            "channels": chan_meta,
            "devices": devices,
            "config_json": json.dumps(config_snapshot
                                      if config_snapshot is not None else {}),
            "name_map": name_map,
        }

        savemat(str(mat_path), mat, format="5", long_field_names=True,
                oned_as="column")
        return mat_path

    @staticmethod
    def _time_axis(blocks: Dict[str, Dict[str, np.ndarray]],
                   rates: Dict[str, float],
                   time_array: Optional[np.ndarray]) -> Optional[np.ndarray]:
        """Explicit array wins; else 0..n/rate for the LONGEST group."""
        if time_array is not None:
            return np.asarray(time_array, dtype=np.float64).ravel()
        best_n, best_rate = 0, 0.0
        for group_name, channels in blocks.items():
            n = max((np.asarray(d).size for d in channels.values()),
                    default=0)
            if n > best_n:
                best_n, best_rate = n, float(rates.get(group_name, 0.0))
        if best_n == 0:
            return None
        dt = (1.0 / best_rate) if best_rate > 0 else 1.0
        return np.arange(best_n, dtype=np.float64) * dt

    # convenience mirror of the module-level helper
    @staticmethod
    def read_point(path: Union[str, Path]) -> Dict[str, Any]:
        return read_point(path)


# ── reader (tests + GUI) ─────────────────────────────────────────────────
def read_point(path: Union[str, Path]) -> Dict[str, Any]:
    """Read one point file back.

    Returns ``{"attrs": {...}, "groups": {group: {ch: np.ndarray}},
    "channel_attrs": {group: {ch: {attr: value}}}, "devices":
    {name: {attr: value}}, "config": dict | None}``.
    """
    out: Dict[str, Any] = {"attrs": {}, "groups": {}, "channel_attrs": {},
                           "devices": {}, "config": None}
    with h5py.File(path, "r") as f:
        out["attrs"] = {k: _from_attr(v) for k, v in f.attrs.items()}
        for group_name, grp in f.items():
            if group_name == "meta":
                cfg = grp.get("config")
                if cfg is not None:
                    raw = cfg[()]
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    out["config"] = json.loads(raw)
                devs = grp.get("devices")
                if devs is not None:
                    for name, sub in devs.items():
                        out["devices"][name] = {
                            k: _from_attr(v) for k, v in sub.attrs.items()}
                continue
            chans: Dict[str, np.ndarray] = {}
            cattrs: Dict[str, Dict[str, Any]] = {}
            for ch_name, dset in grp.items():
                chans[ch_name] = dset[()]
                cattrs[ch_name] = {k: _from_attr(v)
                                   for k, v in dset.attrs.items()}
            out["groups"][group_name] = chans
            out["channel_attrs"][group_name] = cattrs
    return out
