"""Contact sheet of a tool's extracted figures (after native re-crop): python image_sheet.py <final_dir> <out.png> [thumb_h]"""
import sys
import json
import pathlib
from PIL import Image, ImageDraw, ImageFont

final = pathlib.Path(sys.argv[1])
th = int(sys.argv[3]) if len(sys.argv) > 3 else 260
doc = json.loads((final / "book.json").read_text(encoding="utf-8"))
items = [(p["page"], b) for p in doc["pages"] for b in p["blocks"] if b["type"] == "image" and b.get("image")]
try:
    font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 16)
except OSError:
    font = ImageFont.load_default()
thumbs = []
for page, b in items:
    im = Image.open(final / b["image"]).convert("RGB")
    im.thumbnail((th * 2, th))
    canvas = Image.new("RGB", (max(im.width, 150) + 8, th + 30), "white")
    canvas.paste(im, (4, 26))
    d = ImageDraw.Draw(canvas)
    d.rectangle([3, 25, im.width + 4, im.height + 26], outline=(255, 0, 0))
    d.text((4, 2), f"p{page} {b.get('image_px', ['?', '?'])[0]}x{b.get('image_px', ['?', '?'])[1]}px {b.get('image_dpi', '?')}dpi",
           fill=(0, 0, 0), font=font)
    thumbs.append(canvas)
W = 1800
rows, row, x = [], [], 0
for t in thumbs:
    if x + t.width > W and row:
        rows.append(row); row, x = [], 0
    row.append(t); x += t.width + 6
if row:
    rows.append(row)
sheet = Image.new("RGB", (W, sum(max(t.height for t in r) + 6 for r in rows) or 10), (230, 230, 230))
y = 0
for r in rows:
    x = 0
    for t in r:
        sheet.paste(t, (x, y)); x += t.width + 6
    y += max(t.height for t in r) + 6
sheet.save(sys.argv[2])
print(sys.argv[2], len(thumbs), "figures")
