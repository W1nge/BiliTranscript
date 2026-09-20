from __future__ import annotations

"""Stable import location for the single desktop window implementation."""

from .modern_window import ModernMainWindow


MainWindow = ModernMainWindow

__all__ = ["MainWindow", "ModernMainWindow"]
