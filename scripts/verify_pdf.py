"""Acceptance checks for a generated two-layer PDF.

  python verify_pdf.py <out.pdf> <src.pdf> --pages 1-12,99-110 --show 100,104 --search 牟焕奎,西岗区人民政府 --png-dir DIR

- text extraction works and contains no mojibake (share of CJK/ASCII/common punctuation)
- search hits for given terms
- overlay PNGs: every extracted character's box drawn on the page image (what a reader highlights)
- size comparison with the same pages of the source PDF
"""
import re
import json
import argparse
import pathlib
import pymupdf
from PIL import Image, ImageDraw


def parse_ranges(s):
    out = []
    for part in s.split(","):
        a, _, b = part.partition("-")
        out += list(range(int(a), int(b or a) + 1))
    return out


OK = re.compile(r"[　-〿一-鿿＀-￯ -⁯0-9A-Za-z\s\.,;:!?()\[\]%+\-/~·×°<>=\"'*#&@]")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf"); ap.add_argument("src")
    ap.add_argument("--pages", required=True); ap.add_argument("--show", default="")
    ap.add_argument("--search", default=""); ap.add_argument("--png-dir", default=".")
    a = ap.parse_args()
    doc = pymupdf.open(a.pdf)
    orig = parse_ranges(a.pages)
    idx = {p: i for i, p in enumerate(orig)}
    text = "".join(pg.get_text() for pg in doc)
    chars = [c for c in text if not c.isspace()]
    bad = [c for c in chars if not OK.match(c)]
    res = {"pages": doc.page_count, "chars": len(chars), "non_text_chars": len(bad),
           "non_text_sample": "".join(bad[:30]), "bookmarks": len(doc.get_toc()),
           "fonts": sorted({f[3] for pg in doc for f in pg.get_fonts()})}
    hits = {}
    for term in filter(None, a.search.split(",")):
        hits[term] = [orig[i] for i, pg in enumerate(doc) if pg.search_for(term)]
    res["search_hits"] = hits
    out = pathlib.Path(a.png_dir); out.mkdir(parents=True, exist_ok=True)
    for p in filter(None, a.show.split(",")):
        pg = doc[idx[int(p)]]
        z = 110 / 72
        pix = pg.get_pixmap(dpi=110)
        im = Image.frombytes("RGB", (pix.width, pix.height), pix.samples).convert("RGB")
        dr = ImageDraw.Draw(im)
        for b in pg.get_text("rawdict")["blocks"]:          # display-space boxes = what a reader highlights
            for l in b.get("lines", []):
                for s in l["spans"]:
                    for ch in s["chars"]:
                        x0, y0, x1, y1 = ch["bbox"]
                        dr.rectangle([x0 * z, y0 * z, x1 * z, y1 * z], outline=(230, 0, 0))
        im.save(out / f"overlay_p{int(p):04d}.png")
    # size: same pages of the source with old layer vs new
    src = pymupdf.open(a.src)
    sub = pymupdf.open(); [sub.insert_pdf(src, from_page=p - 1, to_page=p - 1) for p in orig]
    sub_bytes = len(sub.tobytes(garbage=3, deflate=True))
    res["out_mb"] = round(pathlib.Path(a.pdf).stat().st_size / 1e6, 3)
    res["src_same_pages_mb"] = round(sub_bytes / 1e6, 3)
    res["size_ratio"] = round(pathlib.Path(a.pdf).stat().st_size / sub_bytes, 3)
    print(json.dumps(res, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
