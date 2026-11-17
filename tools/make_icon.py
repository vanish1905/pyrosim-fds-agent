from __future__ import annotations

import struct
import sys
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QSize, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtSvg import QSvgRenderer


ROOT = Path(__file__).resolve().parents[1]
SVG = ROOT / "assets" / "pyrosim_agent.svg"
ICO = ROOT / "assets" / "pyrosim_agent.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def png_frame(renderer: QSvgRenderer, size: int) -> bytes:
    image = QImage(QSize(size, size), QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(painter)
    painter.end()

    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    image.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def main() -> None:
    renderer = QSvgRenderer(str(SVG))
    if not renderer.isValid():
        raise RuntimeError(f"Invalid SVG icon: {SVG}")
    frames = [(size, png_frame(renderer, size)) for size in SIZES]
    header_size = 6 + 16 * len(frames)
    offset = header_size
    entries = []
    for size, payload in frames:
        dimension = 0 if size == 256 else size
        entries.append(struct.pack("<BBBBHHII", dimension, dimension, 0, 0, 1, 32, len(payload), offset))
        offset += len(payload)
    ICO.parent.mkdir(parents=True, exist_ok=True)
    ICO.write_bytes(struct.pack("<HHH", 0, 1, len(frames)) + b"".join(entries) + b"".join(p for _, p in frames))
    print(f"Generated {ICO} ({ICO.stat().st_size:,} bytes)")


if __name__ == "__main__":
    main()
