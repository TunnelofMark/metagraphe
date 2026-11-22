"""Tile page previews into contact sheets: python contact_sheet.py <preview_dir> <out_prefix> [per_sheet] [thumb_h]"""
import sys
import pathlib
from PIL import Image, ImageDraw

src = pathlib.Path(sys.argv[1])
prefix = sys.argv[2]
per = int(sys.argv[3]) if len(sys.argv) > 3 else 8
th = int(sys.argv[4]) if len(sys.argv) > 4 else 700
files = sorted(src.glob("*.png"))
cols = 4
for s in range(0, len(files), per):
    group = files[s:s + per]
    thumbs = []
    for f in group:
        im = Image.open(f).convert("RGB")
        im = im.resize((round(im.width * th / im.height), th))
        ImageDraw.Draw(im).text((8, 8), f.stem, fill=(255, 0, 0))
        thumbs.append(im)
    tw = max(t.width for t in thumbs)
    rows = (len(thumbs) + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tw, rows * th), "white")
    for i, t in enumerate(thumbs):
        sheet.paste(t, ((i % cols) * tw, (i // cols) * th))
    out = f"{prefix}_{s // per + 1:02d}.png"
    sheet.save(out)
    print(out, [f.stem for f in group])
