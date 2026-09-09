"""Regenerate assets/icon.ico  ->  python make_icon.py"""
import os
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
S = 256
im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)

d.rounded_rectangle([8, 8, S - 8, S - 8], radius=40,
                    fill=(255, 255, 255, 255), outline=(45, 45, 45, 255), width=6)
m = 56
d.line([m, S - m, m, m], fill=(45, 45, 45, 255), width=8)          # y axis
d.line([m, S - m, S - m, S - m], fill=(45, 45, 45, 255), width=8)  # x axis

pts = []
for i in range(101):
    t = i / 100
    x = m + t * (S - 2 * m)
    y = (S - m) - (S - 2 * m) * (0.12 + 0.82 * t ** 1.6)
    pts.append((x, y))
d.line(pts, fill=(30, 95, 200, 255), width=10, joint="curve")

cx, cy = pts[64]
d.line([cx - 34, cy, cx + 34, cy], fill=(220, 30, 30, 255), width=6)
d.line([cx, cy - 34, cx, cy + 34], fill=(220, 30, 30, 255), width=6)
d.ellipse([cx - 15, cy - 15, cx + 15, cy + 15], outline=(220, 30, 30, 255), width=6)

sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
im.save(os.path.join(HERE, "icon.ico"), sizes=sizes)
im.save(os.path.join(HERE, "icon.png"))
print("wrote icon.ico / icon.png")
