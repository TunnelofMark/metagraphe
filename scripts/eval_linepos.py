"""How far off are the text-layer line breaks? Compare the lines build_pdf.py places (block text spread
over detected ink lines) against PP-OCRv5 line OCR (PP-StructureV3 overall_ocr_res: real line text + box).

  python eval_linepos.py <book.json> <ppstructurev3 raw dir> <source pdf> [pages...]
For each placed body-text line matched to an OCR line (same row, IoU >= 0.5), the offset is how many
characters the placed line's start differs from where the OCR line's first characters occur in the
paragraph. Reports the distribution of |offset|.
"""
import re
import sys
import json
import pathlib
import collections
import pymupdf
from textlayer import block_lines, reference_lines

PUNCT = re.compile(r"[\s\W_]", re.U)


def iou(a, b):
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0])); iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    u = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - ix * iy
    return ix * iy / u if u > 0 else 0


def same_row(a, b):
    """Horizontal overlap (fraction of the shorter box) if the vertical centres are within half a line."""
    ca, cb = (a[1] + a[3]) / 2, (b[1] + b[3]) / 2
    if abs(ca - cb) > 0.5 * max(a[3] - a[1], b[3] - b[1]):
        return 0
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    return ix / max(1e-6, min(a[2] - a[0], b[2] - b[0]))


def main():
    book, raw, src = sys.argv[1], pathlib.Path(sys.argv[2]), sys.argv[3]
    want = set(int(x) for x in sys.argv[4:] if x.isdigit())
    doc = json.loads(pathlib.Path(book).read_text(encoding="utf-8"))
    pdf = pymupdf.open(src)
    hist = collections.Counter()
    for p in doc["pages"]:
        if want and p["page"] not in want:
            continue
        f = raw / f"p{p['page']:04d}_res.json"
        if not f.exists():
            continue
        r = json.loads(f.read_text(encoding="utf-8")); r = r.get("res", r)
        sx, sy = p["w"] / r["width"], p["h"] / r["height"]
        ocr = [([b[0] * sx, b[1] * sy, b[2] * sx, b[3] * sy], t) for t, b in
               zip(r["overall_ocr_res"]["rec_texts"], r["overall_ocr_res"]["rec_boxes"])]
        page = pdf[p["page"] - 1]
        refs = reference_lines(page) if "--no-ref" not in sys.argv else None
        for blk in p["blocks"]:
            if blk["type"] not in ("text", "list"):
                continue
            lines = block_lines(page, blk, refs)
            para = "".join(l["text"] for l in lines)
            pos = 0
            for l in lines:
                start = pos
                pos += len(l["text"])
                m = max(ocr, key=lambda o: same_row(o[0], l["bbox"]), default=None)
                if not m or same_row(m[0], l["bbox"]) < 0.5:
                    continue
                head = PUNCT.sub("", m[1])[:3]
                if len(head) < 3:
                    continue
                # where does the OCR line's head occur in the paragraph, nearest to our start?
                cands = [i for i in range(max(0, start - 8), min(len(para), start + 9))
                         if PUNCT.sub("", para[i:i + 6])[:3] == head]
                if not cands:
                    hist["unmatched"] += 1
                    continue
                off = min(cands, key=lambda i: abs(i - start)) - start
                hist[min(abs(off), 4)] += 1
    n = sum(v for k, v in hist.items() if k != "unmatched")
    print({"lines": n, **{f"|off|={k}" if k != 4 else "|off|>=4": f"{v} ({100 * v / max(1, n):.0f}%)"
                          for k, v in sorted((k, v) for k, v in hist.items() if k != "unmatched")},
           "unmatched": hist["unmatched"]})


if __name__ == "__main__":
    main()
