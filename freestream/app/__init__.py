"""Freestream GUI application package (PyQt6 dock-based shell, spec §7)."""

from .main_window import FreestreamMainWindow, build_manager

__all__ = ["FreestreamMainWindow", "build_manager"]
