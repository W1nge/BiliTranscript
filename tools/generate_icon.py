from __future__ import annotations

import io
import sys
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QByteArray, QRectF
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    source = root / "assets" / "bilitranscript.svg"
    png_path = root / "assets" / "bilitranscript.png"
    ico_path = root / "assets" / "bilitranscript.ico"
    app = QGuiApplication(sys.argv[:1])
    renderer = QSvgRenderer(QByteArray(source.read_bytes()))
    image = QImage(512, 512, QImage.Format.Format_ARGB32)
    image.fill(0)
    painter = QPainter(image)
    renderer.render(painter, QRectF(0, 0, 512, 512))
    painter.end()
    image.save(str(png_path), "PNG")
    with Image.open(png_path) as icon:
        icon.save(ico_path, format="ICO", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    app.quit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

