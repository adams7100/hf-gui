"""Draw the HF-Downloader icon and write assets/hf-downloader.ico.

A multi-size .ico (16 to 256 px, PNG-compressed entries) drawn with Qt so no
extra dependency is needed:  .venv\\Scripts\\python make_icon.py
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPolygonF  # noqa: E402

SIZES = (256, 128, 64, 48, 32, 24, 16)
YELLOW = QColor("#FFD21E")  # the Hugging Face yellow
INK = QColor("#1F1F1F")


def render(n: int) -> QImage:
    img = QImage(n, n, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)
    radius = n * 0.22
    p.setBrush(YELLOW)
    p.drawRoundedRect(QRectF(0, 0, n, n), radius, radius)
    p.setBrush(INK)
    cx = n / 2
    shaft_w, top, head_top, tip, head_half = n * 0.17, n * 0.16, n * 0.47, n * 0.72, n * 0.29
    p.drawRect(QRectF(cx - shaft_w / 2, top, shaft_w, head_top - top + n * 0.01))
    p.drawPolygon(QPolygonF([QPointF(cx - head_half, head_top), QPointF(cx + head_half, head_top), QPointF(cx, tip)]))
    p.drawRoundedRect(QRectF(n * 0.19, n * 0.79, n * 0.62, n * 0.075), n * 0.03, n * 0.03)
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def write_ico(path: Path, images: list[QImage]) -> None:
    pngs = [png_bytes(img) for img in images]
    offset = 6 + 16 * len(images)
    entries = b""
    for img, png in zip(images, pngs):
        n = img.width()
        entries += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(struct.pack("<HHH", 0, 1, len(images)) + entries + b"".join(pngs))


def main() -> int:
    QGuiApplication(sys.argv)
    out = Path(__file__).resolve().parent / "assets" / "hf-downloader.ico"
    images = [render(n) for n in SIZES]
    write_ico(out, images)
    images[0].save(str(out.with_suffix(".png")))
    print(f"wrote {out} ({out.stat().st_size} bytes) and {out.with_suffix('.png').name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
