from __future__ import annotations

import argparse
import ctypes
import os
import sys
from pathlib import Path

from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QStyleFactory

from . import __version__
from .main_window import MainWindow
from .styles import APP_STYLE


def resource_path(relative: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[1]))
    return root / relative


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="提取 B站视频文稿的桌面应用")
    parser.add_argument("--version", action="version", version=f"BiliTranscript {__version__}")
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--screenshot", help=argparse.SUPPRESS)
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
    application.setStyleSheet(APP_STYLE)
    icon = resource_path("assets/bilitranscript.ico")
    if icon.exists():
        application.setWindowIcon(QIcon(str(icon)))

    window = MainWindow()
    window.show()
    if args.screenshot:
        def capture() -> None:
            window.grab().save(args.screenshot)
            application.quit()

        QTimer.singleShot(500, capture)
    elif args.smoke_test:
        QTimer.singleShot(700, application.quit)
    return application.exec()


if __name__ == "__main__":
    raise SystemExit(main())
