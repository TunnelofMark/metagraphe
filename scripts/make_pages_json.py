"""Whole-book page index (replaces make_test_subset.py for full runs; the source PDF is only read).

  /opt/ocr/post/bin/python make_pages_json.py <source.pdf> <out pages.json>

Same record shape as make_test_subset.py, for every page: sub_idx = page - 1 (MinerU / layout read the
original PDF directly, so the index into the input is just the page number minus one).
No orig/ or preview/ images are written for full books.
"""
import sys
import json
import pathlib
import pymupdf


def main():
    src = pymupdf.open(sys.argv[1])
    out = pathlib.Path(sys.argv[2])
    out.parent.mkdir(parents=True, exist_ok=True)
    index = []
    for i, page in enumerate(src):
        imgs = page.get_images(full=True)
        rec = {"sub_idx": i, "page": i + 1, "n_images": len(imgs)}
        if imgs:
            xref, _, w, h, bpc, cs, _, _, filt, *_ = imgs[0]
            try:
                bbox = page.get_image_bbox(imgs[0])
                dpi = round(w / (bbox.width / 72)) if bbox.width else None
            except Exception:
                dpi = None
            rec.update(filter=filt, w=w, h=h, bpc=bpc, cs=cs, dpi=dpi)
        index.append(rec)
    out.write_text(json.dumps(index, ensure_ascii=False, indent=1), encoding="utf-8")
    odd = [r["page"] for r in index if r["n_images"] != 1]
    print(json.dumps({"pages": len(index), "pages_not_exactly_one_image": odd[:50],
                      "n_pages_not_exactly_one_image": len(odd)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
