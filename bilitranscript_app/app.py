from __future__ import annotations

import argparse
import ctypes
import os
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QSettings, QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QStyleFactory

from . import __version__
from .history import HistoryStore
from .main_window import MainWindow


def resource_path(relative: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return root / relative


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="提取 B站视频文稿的桌面应用")
    parser.add_argument("--version", action="version", version=f"BiliTranscript {__version__}")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--screenshot", help=argparse.SUPPRESS)
    parser.add_argument("--screenshot-theme", choices=("system", "light", "dark"), help=argparse.SUPPRESS)
    parser.add_argument("--screenshot-page", choices=("home", "history", "settings"), default="home", help=argparse.SUPPRESS)
    parser.add_argument("--startup", action="store_true", help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if sys.platform == "win32":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Winge.BiliTranscript")
        except Exception:
            pass
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    application = QApplication(sys.argv[:1])
    application.setOrganizationName("Winge")
    application.setOrganizationDomain("local")
    application.setApplicationName("BiliTranscript")
    application.setApplicationDisplayName("Bili 文稿")
    application.setApplicationVersion(__version__)
    fusion = QStyleFactory.create("Fusion")
    if fusion:
        application.setStyle(fusion)
    icon = resource_path("assets/bilitranscript.ico")
    if icon.exists():
        application.setWindowIcon(QIcon(str(icon)))

    screenshot_settings_dir = None
    injected_settings = None
    injected_history = None
    if args.screenshot:
        screenshot_settings_dir = tempfile.TemporaryDirectory(prefix="bilitranscript-preview-")
        injected_settings = QSettings(
            str(Path(screenshot_settings_dir.name) / "settings.ini"),
            QSettings.Format.IniFormat,
        )
        if args.screenshot_theme:
            injected_settings.setValue("ui/theme", args.screenshot_theme)
        injected_history = HistoryStore(Path(screenshot_settings_dir.name) / "history.sqlite3")
    window = MainWindow(
        autostart_services=not (args.smoke_test or args.screenshot),
        qsettings=injected_settings,
        history_store=injected_history,
    )
    if args.screenshot:
        page_index = {"home": 0, "history": 1, "settings": 2}[args.screenshot_page]
        window._navigate(page_index)
    window.show()
    if args.screenshot:
        window.raise_()
        window.activateWindow()
        def capture() -> None:
            state = getattr(window, "_acrylic_state", None)
            screen = window.screen() or application.primaryScreen()
            if screen is not None and state is not None and state.applied:
                frame = window.frameGeometry()
                screen_geometry = screen.geometry()
                screen.grabWindow(
                    0,
                    frame.x() - screen_geometry.x(),
                    frame.y() - screen_geometry.y(),
                    frame.width(),
                    frame.height(),
                ).save(args.screenshot)
            else:
                window.grab().save(args.screenshot)
            application.quit()

        QTimer.singleShot(1000, capture)
    elif args.smoke_test:
        QTimer.singleShot(700, application.quit)
    result = application.exec()
    if screenshot_settings_dir is not None:
        screenshot_settings_dir.cleanup()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
