"""Evidence: tool's own crop vs native re-crop of the same figure/table, same detail region enlarged.

  python res_compare.py <tool crop> <native crop> <out.png> [frac_x0 frac_y0 frac_x1 frac_y1]
The detail region is given as fractions of the crop (default: a small patch near the top-left).
"""
import sys
from PIL import Image, ImageDraw, ImageFont

a, b, out = sys.argv[1:4]
fx0, fy0, fx1, fy1 = map(float, sys.argv[4:8]) if len(sys.argv) > 7 else (0.05, 0.05, 0.30, 0.18)
try:
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 20)
except OSError:
    font = ImageFont.load_default()
panels = []
for label, path in (("工具自带截图", a), ("按版面坐标从 PDF 原图重裁", b)):
    im = Image.open(path)
    w, h = im.size
    crop = im.convert("RGB").crop((int(fx0 * w), int(fy0 * h), int(fx1 * w), int(fy1 * h)))
    crop = crop.resize((600, int(600 * crop.height / crop.width)), Image.NEAREST)   # no smoothing: show real pixels
    panels.append((f"{label}：{w}×{h} 像素（{im.mode}）", crop))
H = max(p[1].height for p in panels) + 40
sheet = Image.new("RGB", (sum(p[1].width for p in panels) + 20, H), "white")
x = 0
d = ImageDraw.Draw(sheet)
for label, p in panels:
    d.text((x + 4, 6), label, fill=(0, 0, 0), font=font)
    sheet.paste(p, (x, 40))
    x += p.width + 20
sheet.save(out)
print(out)
