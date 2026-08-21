"""Existing-configuration-folder prompt shown at sweep start.

A configuration name maps to one folder under the data root, so
starting a second sweep with the same name lands on top of the first.
Rather than silently appending run numbers to somebody else's test,
:func:`resolve_config_collision` asks what the operator meant:

repeat
    Keep the existing data and record into a sibling folder with the
    next free letter suffix (``F16``, ``F16_a``, ``F16_b`` ...). The
    measurement config is renamed to match, so every later save, the
    manifest and the window all agree.

overwrite
    Clear the run files and the manifest out of the folder and record
    into it fresh.

cancel
    Do nothing and leave the sweep unstarted.
"""

from __future__ import annotations

import string
from pathlib import Path
from typing import List, Optional, Tuple

from PyQt6.QtWidgets import QMessageBox

#: run artifacts a configuration folder owns; a folder holding none of
#: these is treated as empty and needs no prompt
RUN_SUFFIXES = (".h5", ".mat", ".xlsx", ".tdms")
MANIFEST = "manifest.json"


def existing_runs(config_dir) -> List[Path]:
    """Recorded run files already sitting in a configuration folder."""
    d = Path(config_dir)
    if not d.is_dir():
        return []
    return sorted(p for p in d.iterdir()
                  if p.is_file()
                  and (p.suffix.lower() in RUN_SUFFIXES
                       or p.name == MANIFEST))


def _suffixes():
    """``_a`` ... ``_z``, then ``_aa`` ... ``_zz``."""
    for c in string.ascii_lowercase:
        yield f"_{c}"
    for a in string.ascii_lowercase:
        for b in string.ascii_lowercase:
            yield f"_{a}{b}"


def next_free_name(data_root, config_name: str) -> str:
    """First ``<name><suffix>`` whose folder holds no run files.

    Only the suffix moves, so repeating ``F16`` gives ``F16_a`` even if
    ``F16_a`` was itself created by an earlier repeat.
    """
    root = Path(data_root)
    base = config_name.rstrip("_")
    for suffix in _suffixes():
        candidate = f"{base}{suffix}"
        if not existing_runs(root / candidate):
            return candidate
    raise RuntimeError(f"no free configuration name near {base!r}")


def clear_config_dir(config_dir) -> int:
    """Delete the run files and manifest from a configuration folder.

    Only the artifacts in :data:`RUN_SUFFIXES` plus the manifest go;
    anything else the operator put there (notes, a staged ``.vol``,
    a ``processed`` subfolder) is left alone.
    """
    removed = 0
    for path in existing_runs(config_dir):
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def resolve_config_collision(parent, data_root, config_name: str
                             ) -> Tuple[str, Optional[str]]:
    """Prompt for the collision and report the decision.

    Returns ``(action, new_name)`` where action is ``"proceed"`` (the
    folder was empty, nothing asked), ``"repeat"`` (with the new
    configuration name), ``"overwrite"`` or ``"cancel"``.
    """
    config_dir = Path(data_root) / config_name
    runs = existing_runs(config_dir)
    if not runs:
        return "proceed", None

    n_runs = len([p for p in runs if p.name != MANIFEST])
    proposed = next_free_name(data_root, config_name)

    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("Configuration folder already exists")
    box.setText(f"<b>{config_name}</b> already holds {n_runs} recorded "
                f"run file(s).")
    box.setInformativeText(
        f"<p><b>Repeat</b> keeps that data and records into "
        f"<b>{proposed}</b> instead. The measurement configuration is "
        f"renamed to match.</p>"
        f"<p><b>Overwrite</b> deletes the {n_runs} run file(s) and the "
        f"manifest in {config_name}, then records fresh.</p>")
    box.setDetailedText(str(config_dir) + "\n\n"
                        + "\n".join(p.name for p in runs))
    repeat_btn = box.addButton(f"Repeat as {proposed}",
                               QMessageBox.ButtonRole.AcceptRole)
    over_btn = box.addButton("Overwrite", QMessageBox.ButtonRole.
                             DestructiveRole)
    cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(repeat_btn)
    box.exec()

    clicked = box.clickedButton()
    if clicked is repeat_btn:
        return "repeat", proposed
    if clicked is over_btn:
        confirm = QMessageBox.warning(
            parent, "Delete recorded data?",
            f"Delete {n_runs} run file(s) and the manifest from "
            f"{config_name}?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if confirm == QMessageBox.StandardButton.Yes:
            return "overwrite", None
        return "cancel", None
    if clicked is cancel_btn:
        return "cancel", None
    return "cancel", None
