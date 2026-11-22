"""Fix MinerU figure regions with PP-DocLayoutV3 image boxes (calligraphy / inscriptions read as
headings, photo banners split off as titles).

  python hybrid_images.py <mineru doc.json> <layout_dir> <out doc.json> [--log log.json]

layout_dir holds pNNNN_res.json files containing `layout_det_res.boxes` (label, coordinate, in the
pixel space given by the file's width/height) -- written by PaddleOCR-VL, or by the stand-alone
`layout_detection` run in run_layout.py for the full books.

Rule, per layout box labelled image (area >= 0.5 % of the page):
  MinerU title/text/caption blocks lying >= 80 % inside the box are removed from the text flow
  (their OCR text is kept as `inner_text` of the image). If a MinerU image overlaps the box, it is
  grown to the union; otherwise a new image block takes the place of the first removed block.
Every change is logged so it can be reviewed.
"""
import sys
import json
import argparse
import pathlib


def inside_frac(a, b):
    """Fraction of box a's area that lies inside box b."""
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    area = max(1e-6, (a[2] - a[0]) * (a[3] - a[1]))
    return ix * iy / area


def layout_images(layout_dir, page, w_pt, h_pt):
    f = pathlib.Path(layout_dir) / f"p{page:04d}_res.json"
    if not f.exists():
        return None
    r = json.loads(f.read_text(encoding="utf-8"))
    r = r.get("res", r)
    W, H = r["width"], r["height"]
    sx, sy = w_pt / W, h_pt / H
    out = []
    for b in (r.get("layout_det_res") or {}).get("boxes", []):
        if b.get("label") in ("image", "chart", "seal"):
            x0, y0, x1, y1 = b["coordinate"][:4]
            box = [x0 * sx, y0 * sy, x1 * sx, y1 * sy]
            if (box[2] - box[0]) * (box[3] - box[1]) >= 0.005 * w_pt * h_pt:
                out.append({"bbox": box, "score": round(b.get("score", 0), 3)})
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("doc"); ap.add_argument("layout_dir"); ap.add_argument("out")
    ap.add_argument("--log")
    a = ap.parse_args()
    doc = json.loads(pathlib.Path(a.doc).read_text(encoding="utf-8"))
    log = []
    for p in doc["pages"]:
        boxes = layout_images(a.layout_dir, p["page"], p["w"], p["h"])
        if not boxes:
            continue
        for lb in boxes:
            B = lb["bbox"]
            victims = [i for i, b in enumerate(p["blocks"])
                       if b["type"] in ("title", "text", "caption") and not b.get("parent")
                       and inside_frac(b["bbox"], B) >= 0.8]
            if not victims:
                continue
            imgs = [b for b in p["blocks"] if b["type"] == "image" and inside_frac(b["bbox"], B) > 0.3]
            removed = [p["blocks"][i] for i in victims]
            inner = " ".join((b.get("text") or "").strip() for b in removed)
            if imgs:
                img = imgs[0]
                old = list(img["bbox"])
                img["bbox"] = [min(img["bbox"][0], B[0]), min(img["bbox"][1], B[1]),
                               max(img["bbox"][2], B[2]), max(img["bbox"][3], B[3])]
                img["inner_text"] = (img.get("inner_text", "") + " " + inner).strip()
                img.pop("image", None)                     # force re-crop
                action = {"action": "grow_image", "from": [round(v) for v in old], "to": [round(v) for v in img["bbox"]]}
                p["blocks"] = [b for i, b in enumerate(p["blocks"]) if i not in victims]
            else:
                new = {"type": "image", "level": None, "bbox": [round(v, 2) for v in B], "text": "", "html": None,
                       "lines": None, "image": None, "inner_text": inner, "source": "PP-DocLayoutV3"}
                keep = [b for i, b in enumerate(p["blocks"]) if i not in victims]
                keep.insert(victims[0] - sum(1 for v in victims if v < victims[0]), new)
                p["blocks"] = keep
                action = {"action": "new_image", "bbox": [round(v) for v in B]}
            action.update(page=p["page"], removed_text=[(b["type"], (b.get("text") or "")[:40]) for b in removed],
                          layout_score=lb["score"])
            log.append(action)
    doc["hybrid_images"] = log
    pathlib.Path(a.out).write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    if a.log:
        pathlib.Path(a.log).write_text(json.dumps(log, ensure_ascii=False, indent=1), encoding="utf-8")
    for l in log:
        print(json.dumps(l, ensure_ascii=False))


if __name__ == "__main__":
    main()
