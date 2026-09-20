from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QMessageBox

from bilitranscript_app.history import HistoryStore
from bilitranscript_app.main_window import MainWindow


class MainWindowStartupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def make_window(self) -> MainWindow:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        with patch("bilitranscript_app.modern_window.is_startup_enabled", return_value=False):
            return MainWindow(
                autostart_services=False,
                qsettings=QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat),
                history_store=HistoryStore(root / "history.sqlite3"),
            )

    def test_normal_window_schedules_api_and_browser_autostart(self) -> None:
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch("bilitranscript_app.modern_window.is_startup_enabled", return_value=False),
            patch.object(MainWindow, "_migrate_automatic_services") as migrate,
            patch("bilitranscript_app.modern_window.QTimer.singleShot") as single_shot,
        ):
            root = Path(temporary)
            window = MainWindow(
                autostart_services=True,
                qsettings=QSettings(str(root / "settings.ini"), QSettings.Format.IniFormat),
                history_store=HistoryStore(root / "history.sqlite3"),
            )
        try:
            migrate.assert_called_once_with()
            callbacks = [call.args[1].__name__ for call in single_shot.call_args_list]
            self.assertEqual(callbacks, ["_auto_start_extraction_api", "_auto_open_login_browser"])
        finally:
            window.close()

    def test_automatic_browser_launch_checks_login_without_dialog(self) -> None:
        window = self.make_window()
        try:
            window._browser_launch_automatic = True
            window._login_watch_active = True
            window._login_watch_checks_remaining = 10
            with (
                patch.object(window, "_schedule_login_check") as schedule,
                patch.object(QMessageBox, "information") as information,
            ):
                window._browser_opened("Microsoft Edge")
            schedule.assert_called_once_with(500)
            information.assert_not_called()
            self.assertIn("正在检查登录", window.status_label.text())
        finally:
            window.close()

    def test_login_watch_rechecks_until_account_is_ready(self) -> None:
        window = self.make_window()
        try:
            window._login_watch_active = True
            window._login_watch_checks_remaining = 10
            with patch.object(window, "_schedule_login_check") as schedule:
                window._login_status_ready(False, "尚未登录 B站")
                schedule.assert_called_once_with()
            self.assertTrue(window._login_watch_active)

            window._login_status_ready(True, "tester")
            self.assertFalse(window._login_watch_active)
            self.assertIn("tester", window.login_status_label.text())
        finally:
            window.close()


if __name__ == "__main__":
    unittest.main()
