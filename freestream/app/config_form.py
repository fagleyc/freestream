"""ConfigForm — reflection-driven editor covering EVERY config field.

The device settings dialogs shipped with the drivers expose only a curated
subset of each config. Freestream's device configuration must be complete
(communication properties, sampling frequencies, protocol/word-order,
safety limits, …), so this form walks a config *dataclass instance* and
generates a grouped editor for every scalar field:

* ``bool`` → QCheckBox, ``int`` → QSpinBox, ``float`` → QDoubleSpinBox,
  ``str`` → QLineEdit (or a QComboBox when the field has a known choice
  list, e.g. tunnel ``word_order``).
* Fields are grouped into titled sections (Communication, Sampling, …) via
  a ``{section: [field, …]}`` mapping; unmapped fields land in "Other" so a
  driver gaining a new option automatically shows up here.
* Unit suffixes are inferred from the field-name convention the drivers
  already use (``…_hz``, ``…_ms``, ``…_s``, ``…_deg``, ``…_v``, ``…_in``).
* Nested dataclasses / lists are *skipped* — those get dedicated tabs
  (axis forms, channel tables) in :mod:`freestream.app.device_config`.

Edits stay in the widgets until :meth:`apply` writes them back onto the
live object (so a dialog Cancel is a true no-op), and :meth:`load`
refreshes the widgets from the object (after a config-file load).
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from PyQt6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox,
                             QFormLayout, QGroupBox, QLineEdit, QScrollArea,
                             QSpinBox, QVBoxLayout, QWidget)

# field-name suffix → widget unit suffix
_UNIT_SUFFIXES = (
    ("_hz", " Hz"), ("_ms", " ms"), ("_seconds", " s"),
    ("_steps_s", " steps/s"), ("_s", " s"),
    ("_deg", " °"), ("_mv", " mV"), ("_v", " V"), ("_in", " in"),
    ("_m2", " m²"), ("_m", " m"), ("_kg_m3", " kg/m³"), ("_pa", " Pa"),
)

#: fields never shown — sim/live is governed by Freestream's own switch,
#: and channel/axis containers get dedicated tabs.
DEFAULT_SKIP = ("force_sim",)


def _label(name: str) -> str:
    for suffix, _unit in _UNIT_SUFFIXES:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
            break
    return name.replace("_", " ")


def _unit(name: str) -> str:
    for suffix, unit in _UNIT_SUFFIXES:
        if name.endswith(suffix):
            return unit
    return ""


class ConfigForm(QWidget):
    """Auto-generated grouped editor for one config dataclass instance."""

    def __init__(self, obj: Any,
                 sections: Optional[Sequence[Tuple[str, Sequence[str]]]] = None,
                 choices: Optional[Dict[str, Sequence[str]]] = None,
                 skip: Sequence[str] = (),
                 parent=None):
        super().__init__(parent)
        self._obj = obj
        self._choices = dict(choices or {})
        self._skip = set(DEFAULT_SKIP) | set(skip)
        #: field name → (widget, getter, setter)
        self._editors: Dict[str, Tuple[QWidget, Callable, Callable]] = {}

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.Shape.NoFrame)
        host = QWidget()
        self._vbox = QVBoxLayout(host)
        self._vbox.setSpacing(8)
        scroll.setWidget(host)
        outer.addWidget(scroll)

        editable = self._editable_fields()
        placed: set = set()
        for title, names in (sections or ()):
            group_names = [n for n in names if n in editable]
            if group_names:
                self._add_group(title, group_names)
                placed.update(group_names)
        rest = [n for n in editable if n not in placed]
        if rest:
            self._add_group("Other" if placed else "Settings", rest)
        self._vbox.addStretch(1)
        self.load()

    # ── construction ─────────────────────────────────────────────────────
    def _editable_fields(self) -> List[str]:
        names: List[str] = []
        for f in dataclasses.fields(self._obj):
            if f.name in self._skip:
                continue
            value = getattr(self._obj, f.name)
            if isinstance(value, (bool, int, float, str)):
                names.append(f.name)
        return names

    def _add_group(self, title: str, names: Sequence[str]) -> None:
        box = QGroupBox(title)
        form = QFormLayout(box)
        form.setHorizontalSpacing(12)
        form.setVerticalSpacing(6)
        for name in names:
            widget, getter, setter = self._make_editor(name)
            self._editors[name] = (widget, getter, setter)
            form.addRow(_label(name), widget)
        self._vbox.addWidget(box)

    def _make_editor(self, name: str):
        value = getattr(self._obj, name)
        if name in self._choices:
            combo = QComboBox()
            items = [str(c) for c in self._choices[name]]
            combo.addItems(items)
            return combo, combo.currentText, combo.setCurrentText
        if isinstance(value, bool):                    # BEFORE int check
            chk = QCheckBox()
            return chk, chk.isChecked, chk.setChecked
        if isinstance(value, int):
            spin = QSpinBox()
            spin.setRange(-2_000_000_000, 2_000_000_000)
            spin.setSuffix(_unit(name))
            spin.setGroupSeparatorShown(True)
            return spin, spin.value, spin.setValue
        if isinstance(value, float):
            dspin = QDoubleSpinBox()
            dspin.setRange(-1e12, 1e12)
            dspin.setDecimals(4)
            dspin.setSuffix(_unit(name))
            return dspin, dspin.value, dspin.setValue
        edit = QLineEdit()
        return edit, edit.text, lambda v: edit.setText(str(v))

    # ── data flow ────────────────────────────────────────────────────────
    def load(self) -> None:
        """Widgets ← object (initial fill / after a config-file load)."""
        for name, (_w, _get, setter) in self._editors.items():
            setter(getattr(self._obj, name))

    def apply(self) -> None:
        """Object ← widgets (dialog OK / Apply). Values are coerced back to
        the field's current runtime type so int fields stay ints."""
        for name, (_w, getter, _set) in self._editors.items():
            old = getattr(self._obj, name)
            new = getter()
            if isinstance(old, bool):
                new = bool(new)
            elif isinstance(old, int) and not isinstance(old, bool):
                new = int(new)
            elif isinstance(old, float):
                new = float(new)
            else:
                new = type(old)(new) if not isinstance(old, str) else \
                    str(new).strip()
            setattr(self._obj, name, new)

    def fields(self) -> List[str]:
        return list(self._editors)
