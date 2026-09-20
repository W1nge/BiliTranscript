from __future__ import annotations

"""Low-overhead Windows Composition backdrop used by the Qt shell.

The Qt window stays responsible for controls and translucent color layers.  A
small native Win32 window directly behind it owns the system-composited blur,
so moving and resizing the app does not repeatedly invalidate a Qt acrylic
surface.  Importing this module is harmless on unsupported systems.
"""

import ctypes
import sys
import threading
from pathlib import Path
from typing import Any


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class WINDOWPOS(ctypes.Structure):
    _fields_ = [
        ("hwnd", ctypes.c_void_p),
        ("hwndInsertAfter", ctypes.c_void_p),
        ("x", ctypes.c_int),
        ("y", ctypes.c_int),
        ("cx", ctypes.c_int),
        ("cy", ctypes.c_int),
        ("flags", ctypes.c_uint),
    ]


def _runtime_directory() -> Path:
    candidates = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(bundle_root) / "bilitranscript_app" / "native" / "win-x64")
        candidates.append(Path(bundle_root) / "native" / "win-x64")
    candidates.append(Path(__file__).resolve().parent / "native" / "win-x64")
    for candidate in candidates:
        if (candidate / "BiliTranscript.Backdrop.dll").is_file():
            return candidate
    return candidates[0]


def composition_backdrop_available() -> bool:
    return sys.platform == "win32" and (_runtime_directory() / "BiliTranscript.Backdrop.dll").is_file()


def _tint_for_theme(theme: str) -> int:
    # AARRGGBB.  Qt adds the page/sidebar color hierarchy above this one blur.
    return 0x780C1420 if theme == "dark" else 0x68F4F8FF


_runtime_lock = threading.Lock()
_runtime_dependencies: tuple[Any, ...] | None = None
_runtime_library: Any | None = None


def preload_composition_runtime() -> None:
    """Load NativeAOT and Win2D off the GUI thread once per process."""
    global _runtime_dependencies, _runtime_library
    if _runtime_library is not None:
        return
    with _runtime_lock:
        if _runtime_library is not None:
            return
        runtime = _runtime_directory()
        bridge = runtime / "BiliTranscript.Backdrop.dll"
        dependencies: list[Any] = []
        for name in (
            "msvcp140_app.dll",
            "vcruntime140_1_app.dll",
            "vcruntime140_app.dll",
            "Microsoft.Graphics.Canvas.dll",
        ):
            path = runtime / name
            if not path.is_file():
                raise RuntimeError(f"Acrylic runtime file is missing: {name}")
            dependencies.append(ctypes.WinDLL(str(path)))
        if not bridge.is_file():
            raise RuntimeError("Acrylic runtime file is missing: BiliTranscript.Backdrop.dll")
        library = ctypes.CDLL(str(bridge))
        library.bt_backdrop_create.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_float, ctypes.c_float]
        library.bt_backdrop_create.restype = ctypes.c_void_p
        library.bt_backdrop_sync.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        library.bt_backdrop_sync.restype = ctypes.c_int
        library.bt_backdrop_show.argtypes = [ctypes.c_void_p, ctypes.c_int]
        library.bt_backdrop_show.restype = ctypes.c_int
        library.bt_backdrop_update.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_float, ctypes.c_float]
        library.bt_backdrop_update.restype = ctypes.c_int
        library.bt_backdrop_destroy.argtypes = [ctypes.c_void_p]
        library.bt_backdrop_destroy.restype = None
        library.bt_backdrop_last_error.argtypes = [ctypes.c_void_p, ctypes.c_int]
        library.bt_backdrop_last_error.restype = ctypes.c_int
        _runtime_dependencies = tuple(dependencies)
        _runtime_library = library


class CompositionBackdrop:
    """Own and synchronize the native compositor backdrop for one QWidget."""

    def __init__(self, window: Any, *, theme: str) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows Composition is unavailable on this platform")
        self._window = window
        self._runtime = _runtime_directory()
        self._dependencies: list[Any] = []
        self._library: Any | None = None
        self._token: int | None = None
        self._closed = False
        self._load_runtime()
        assert self._library is not None
        token = self._library.bt_backdrop_create(
            int(window.winId()),
            _tint_for_theme(theme),
            ctypes.c_float(46.0),
            ctypes.c_float(1.16),
        )
        if not token:
            raise RuntimeError(self._last_error() or "Windows Composition backdrop creation failed")
        self._token = int(token)

    def _load_runtime(self) -> None:
        preload_composition_runtime()
        self._dependencies = list(_runtime_dependencies or ())
        self._library = _runtime_library
        if self._library is None:
            raise RuntimeError("Acrylic runtime did not load")
        library = self._library

    def _last_error(self) -> str:
        if self._library is None:
            return ""
        buffer = ctypes.create_string_buffer(8192)
        self._library.bt_backdrop_last_error(buffer, len(buffer))
        return buffer.value.decode("utf-8", errors="replace").strip()

    def window_rect(self) -> tuple[int, int, int, int] | None:
        if self._closed:
            return None
        rect = RECT()
        if not ctypes.windll.user32.GetWindowRect(int(self._window.winId()), ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top

    def sync_to_window(self, *, sync_z_order: bool = False) -> bool:
        rect = self.window_rect()
        return bool(rect and self.sync_rect(*rect, sync_z_order=sync_z_order))

    def sync_rect(self, x: int, y: int, width: int, height: int, *, sync_z_order: bool = False) -> bool:
        if self._closed or self._library is None or self._token is None:
            return False
        if width <= 0 or height <= 0:
            return False
        return bool(self._library.bt_backdrop_sync(self._token, x, y, width, height, int(sync_z_order)))

    def set_visible(self, visible: bool) -> None:
        if not self._closed and self._library is not None and self._token is not None:
            self._library.bt_backdrop_show(self._token, int(visible))

    def update_theme(self, theme: str) -> None:
        if not self._closed and self._library is not None and self._token is not None:
            self._library.bt_backdrop_update(self._token, _tint_for_theme(theme), 46.0, 1.16)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._library is not None and self._token is not None:
            self._library.bt_backdrop_destroy(self._token)
        self._token = None


__all__ = ["CompositionBackdrop", "WINDOWPOS", "composition_backdrop_available", "preload_composition_runtime"]
