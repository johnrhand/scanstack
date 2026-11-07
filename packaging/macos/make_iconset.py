"""Render the SVG icon into a macOS .iconset folder (iconutil turns it into .icns)."""
import os
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter
from PySide6.QtSvg import QSvgRenderer

svg, out = sys.argv[1], sys.argv[2]
app = QGuiApplication(["x"])
r = QSvgRenderer(svg)
if not r.isValid():
    sys.exit(f"Can't read {svg}")
os.makedirs(out, exist_ok=True)
for base in (16, 32, 128, 256, 512):
    for scale in (1, 2):
        px = base * scale
        img = QImage(px, px, QImage.Format_ARGB32)
        img.fill(Qt.transparent)
        p = QPainter(img)
        p.setRenderHint(QPainter.Antialiasing)
        r.render(p)
        p.end()
        name = f"icon_{base}x{base}{'@2x' if scale == 2 else ''}.png"
        img.save(os.path.join(out, name))
print("iconset ready:", out)
