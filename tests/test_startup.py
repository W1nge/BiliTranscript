from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from bilitranscript_app import startup


class StartupTests(unittest.TestCase):
    def test_frozen_command_quotes_executable_and_marks_startup(self) -> None:
        command = startup.startup_command(
            executable=Path(r"C:\Program Files\BiliTranscript\BiliTranscript.exe"),
            frozen=True,
        )
        self.assertEqual(command, '"C:\\Program Files\\BiliTranscript\\BiliTranscript.exe" --startup')

    def test_source_command_includes_launcher(self) -> None:
        command = startup.startup_command(
            executable=Path(r"C:\Python\python.exe"),
            frozen=False,
            script_path=Path(r"C:\Source Folder\bilitranscript.py"),
        )
        self.assertEqual(
            command,
            'C:\\Python\\python.exe "C:\\Source Folder\\bilitranscript.py" --startup',
        )

    def test_enabled_requires_current_command(self) -> None:
        registry = MagicMock()
        registry.HKEY_CURRENT_USER = object()
        registry.KEY_QUERY_VALUE = 1
        registry.REG_SZ = 1
        registry.OpenKey.return_value.__enter__.return_value = object()
        registry.QueryValueEx.return_value = ('"C:\\App\\BiliTranscript.exe" --startup', registry.REG_SZ)
        with patch.object(startup, "winreg", registry):
            self.assertTrue(startup.is_startup_enabled(command='"C:\\App\\BiliTranscript.exe" --startup'))
            self.assertFalse(startup.is_startup_enabled(command='"D:\\App\\BiliTranscript.exe" --startup'))

    def test_set_and_clear_registry_value(self) -> None:
        registry = MagicMock()
        registry.HKEY_CURRENT_USER = object()
        registry.KEY_SET_VALUE = 2
        registry.REG_SZ = 1
        key = object()
        registry.CreateKeyEx.return_value.__enter__.return_value = key
        with patch.object(startup, "winreg", registry):
            startup.set_startup_enabled(True, command="app-command")
            registry.SetValueEx.assert_called_once_with(
                key,
                startup.RUN_VALUE_NAME,
                0,
                registry.REG_SZ,
                "app-command",
            )
            startup.set_startup_enabled(False)
            registry.DeleteValue.assert_called_once_with(key, startup.RUN_VALUE_NAME)


if __name__ == "__main__":
    unittest.main()
