"""Generate assets/app.ico (NinjaAlert tray/exe icon) using QPainter.

Run:  python tools/make_icon.py
Requires PyQt6 (installed with requirements.txt). Purely optional - the
runtime tray icon is drawn in code; this file only beautifies the .exe.
"""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtWidgets import QApplication


def render(size: int) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(QColor(0, 0, 0, 0))
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 64.0
    p.setBrush(QColor("#12151d"))
    pen = QPen(QColor("#3a4354"))
    pen.setWidthF(2.0 * s)
    p.setPen(pen)
    p.drawRoundedRect(int(2 * s), int(2 * s), int(size - 4 * s), int(size - 4 * s),
                      int(14 * s), int(14 * s))
    pen = QPen(QColor("#41d98c"))
    pen.setWidthF(5.0 * s)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    p.setPen(pen)
    points = [(12 * s, 46 * s), (22 * s, 46 * s), (28 * s, 32 * s),
              (36 * s, 38 * s), (50 * s, 16 * s)]
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        p.drawLine(int(x1), int(y1), int(x2), int(y2))
    p.drawLine(int(50 * s), int(16 * s), int(40 * s), int(17 * s))
    p.drawLine(int(50 * s), int(16 * s), int(49 * s), int(26 * s))
    p.end()
    return pix


if __name__ == "__main__":
    app = QApplication(sys.argv)  # required for QPainter on some platforms
    out_dir = os.path.join(os.path.dirname(__file__), "..", "assets")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.abspath(os.path.join(out_dir, "app.ico"))
    icon = QIcon()
    for sz in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(render(sz))
    ok = icon.pixmap(256).save(out_path, "ICO")
    print(("wrote " if ok else "FAILED ") + out_path)
    sys.exit(0 if ok else 1)
