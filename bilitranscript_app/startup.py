from __future__ import annotations

import subprocess
import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover - BiliTranscript is distributed for Windows.
    winreg = None  # type: ignore[assignment]


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE_NAME = "BiliTranscript"


class StartupError(RuntimeError):
    pass


def startup_command(
    *,
    executable: Path | None = None,
    frozen: bool | None = None,
    script_path: Path | None = None,
) -> str:
    """Return the Windows Run command for the current app installation."""

    executable = Path(executable or sys.executable).resolve()
    frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
    if frozen:
        arguments = [str(executable), "--startup"]
    else:
        if executable.name.lower() == "python.exe":
            pythonw = executable.with_name("pythonw.exe")
            if pythonw.is_file():
                executable = pythonw
        launcher = Path(script_path or (Path(__file__).resolve().parents[1] / "bilitranscript.py")).resolve()
        arguments = [str(executable), str(launcher), "--startup"]
    return subprocess.list2cmdline(arguments)


def is_startup_enabled(*, command: str | None = None) -> bool:
    if winreg is None:
        return False
    expected = command or startup_command()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_QUERY_VALUE) as key:
            value, value_type = winreg.QueryValueEx(key, RUN_VALUE_NAME)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise StartupError(f"无法读取 Windows 开机启动项：{exc}") from exc
    return value_type == winreg.REG_SZ and str(value).strip() == expected.strip()


def set_startup_enabled(enabled: bool, *, command: str | None = None) -> None:
    if winreg is None:
        raise StartupError("当前系统不支持 Windows 开机自启")
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, RUN_VALUE_NAME, 0, winreg.REG_SZ, command or startup_command())
            else:
                try:
                    winreg.DeleteValue(key, RUN_VALUE_NAME)
                except FileNotFoundError:
                    pass
    except OSError as exc:
        action = "设置" if enabled else "关闭"
        raise StartupError(f"{action}开机自启失败：{exc}") from exc
