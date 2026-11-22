"""Side-by-side evidence image: original page | each tool's detected blocks drawn on the page.

  python viz_layout.py <book> <page> <out.png> [tool ...]
Colors: title=red, text/list=blue, image=green (thick), table=orange (thick), caption=purple,
header/footer/page_number/aside=grey. Block order numbers are drawn at the top-left corner.
"""
import sys
import json
import pathlib
import pymupdf
from PIL import Image, ImageDraw, ImageFont

ROOT = pathlib.Path(__file__).resolve().parent.parent / "ocr_test"
BOOKS = json.loads((pathlib.Path(__file__).parent / "books.json").read_text(encoding="utf-8"))
COL = {"title": (220, 0, 0), "text": (0, 90, 220), "list": (0, 90, 220), "toc": (0, 90, 220),
       "image": (0, 160, 0), "table": (240, 120, 0), "caption": (150, 0, 180), "footnote": (0, 150, 150)}
GREY = (140, 140, 140)
NAMES = {"mineru_advanced": "MinerU 4.0.12", "paddleocr_vl": "PaddleOCR-VL 1.6", "ppstructurev3": "PP-StructureV3",
         "oldtext": "超星旧文字层"}


def font(size):
    for f in ("C:/Windows/Fonts/msyh.ttc", "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"):
        try:
            return ImageFont.truetype(f, size)
        except OSError:
            pass
    return ImageFont.load_default()


def main():
    book, page, out = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    tools = sys.argv[4:] or ["mineru_advanced", "paddleocr_vl", "ppstructurev3"]
    pdf = pymupdf.open(BOOKS[book]["pdf"])
    pg = pdf[page - 1]
    z = 1.3
    pix = pg.get_pixmap(matrix=pymupdf.Matrix(z, z))
    base = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    panels = [("原页", base.copy())]
    for t in tools:
        f = ROOT / t / book / "doc_hybrid.json"
        if not f.exists():
            f = ROOT / t / book / "doc.json"
        im = base.copy()
        d = ImageDraw.Draw(im)
        if f.exists():
            doc = json.loads(f.read_text(encoding="utf-8"))
            p = next((p for p in doc["pages"] if p["page"] == page), None)
            for k, b in enumerate(p["blocks"] if p else []):
                c = COL.get(b["type"], GREY)
                w = 4 if b["type"] in ("image", "table") else 2
                for bb in b.get("bboxes") or [b["bbox"]]:
                    x0, y0, x1, y1 = [v * z for v in bb]
                    d.rectangle([x0, y0, x1, y1], outline=c, width=w)
                d.text((b["bbox"][0] * z + 2, b["bbox"][1] * z + 1), str(k), fill=c, font=font(14))
        panels.append((NAMES.get(t, t), im))
    W, H = base.size
    sheet = Image.new("RGB", (W * len(panels) + 10 * (len(panels) - 1), H + 40), "white")
    dr = ImageDraw.Draw(sheet)
    for i, (name, im) in enumerate(panels):
        x = i * (W + 10)
        sheet.paste(im, (x, 40))
        dr.text((x + 8, 6), f"{name}  ·  {BOOKS[book]['title']} p{page}", fill=(0, 0, 0), font=font(22))
    sheet.save(out)
    print(out, sheet.size)


if __name__ == "__main__":
    main()
