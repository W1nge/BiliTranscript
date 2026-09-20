from __future__ import annotations

"""Small, event-driven Windows material capability and fallback helpers.

The active compositor backend lives in :mod:`composition_backdrop`.  This
module only resolves themes, checks accessibility/power preferences, and can
clear the retired accent effect.  It never takes screenshots or starts a timer.
"""

import ctypes
import sys
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class AcrylicState:
    requested: bool
    applied: bool
    theme: str
    reason: str = ""


def system_prefers_dark() -> bool:
    """Read the Windows light/dark preference without polling."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
            return int(value) == 0
    except Exception:
        return False


def effective_theme(theme: str = "system") -> str:
    value = str(theme or "system").lower()
    if value == "dark":
        return "dark"
    if value == "light":
        return "light"
    return "dark" if system_prefers_dark() else "light"


def _transparency_allowed() -> bool:
    if sys.platform != "win32":
        return False
    # High contrast and battery saver intentionally use an opaque fallback.
    try:
        user32 = ctypes.windll.user32
        class HIGHCONTRAST(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwFlags", ctypes.c_uint), ("lpszDefaultScheme", ctypes.c_wchar_p)]

        high = HIGHCONTRAST(ctypes.sizeof(HIGHCONTRAST), 0, None)
        if user32.SystemParametersInfoW(0x0042, ctypes.sizeof(high), ctypes.byref(high), 0) and high.dwFlags & 0x1:
            return False
    except Exception:
        pass
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            value, _ = winreg.QueryValueEx(key, "EnableTransparency")
            if int(value) == 0:
                return False
    except Exception:
        pass
    try:
        class SYSTEM_POWER_STATUS(ctypes.Structure):
            _fields_ = [
                ("ACLineStatus", ctypes.c_ubyte),
                ("BatteryFlag", ctypes.c_ubyte),
                ("BatteryLifePercent", ctypes.c_ubyte),
                ("SystemStatusFlag", ctypes.c_ubyte),
                ("BatteryLifeTime", ctypes.c_ulong),
                ("BatteryFullLifeTime", ctypes.c_ulong),
            ]

        power = SYSTEM_POWER_STATUS()
        if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power)) and power.SystemStatusFlag == 1:
            return False
    except Exception:
        pass
    # Windows power API is optional; inability to query it should not disable Acrylic.
    return True


def acrylic_supported() -> bool:
    return sys.platform == "win32" and _transparency_allowed() and hasattr(ctypes, "windll")


__all__ = ["AcrylicState", "acrylic_supported", "effective_theme", "system_prefers_dark"]
