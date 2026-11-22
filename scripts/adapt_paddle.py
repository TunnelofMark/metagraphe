"""PaddleOCR-VL / PP-StructureV3 per-page *_res.json -> normalized doc.json.

  python adapt_paddle.py <tool_out_dir> <pages.json>
  reads <tool_out_dir>/raw/pNNNN_res.json, writes <tool_out_dir>/doc.json

- block_bbox is in pixels of the page image the pipeline rendered; scaled to display-space points.
- PP-StructureV3 also has line-level OCR (overall_ocr_res); those lines are attached to the text
  blocks that contain them, so the PDF text layer can use real line geometry.
- figure_title / table_title blocks are attached to the nearest image / table below or above.
"""
import re
import sys
import json
import pathlib
import pymupdf
from textlayer import clean_text

LABEL = {"header": "header", "header_image": "header", "footer": "footer", "footer_image": "footer",
         "number": "page_number", "doc_title": "title", "paragraph_title": "title",
         "text": "text", "abstract": "text", "content": "toc", "reference": "text", "reference_content": "text",
         "algorithm": "text", "formula": "text", "formula_number": "text", "list": "list",
         "figure_title": "caption", "table_title": "caption", "chart_title": "caption",
         "image": "image", "chart": "image", "seal": "image", "table": "table",
         "footnote": "footnote", "vision_footnote": "caption", "aside_text": "aside", "vertical_text": "text"}


def overlap_x(a, b):
    return max(0, min(a[2], b[2]) - max(a[0], b[0])) / max(1e-6, min(a[2] - a[0], b[2] - b[0]))


def attach_captions(blocks):
    figs = [b for b in blocks if b["type"] in ("image", "table")]
    for c in [b for b in blocks if b["type"] == "caption"]:
        best, dist = None, 1e9
        for f in figs:
            if overlap_x(c["bbox"], f["bbox"]) < 0.3 and not (abs(c["bbox"][1] - f["bbox"][1]) < 5):
                continue
            d = min(abs(c["bbox"][1] - f["bbox"][3]), abs(f["bbox"][1] - c["bbox"][3]))
            if d < dist:
                best, dist = f, d
        if best is not None and dist < 40:
            best["caption"] = (best.get("caption", "") + " " + c["text"]).strip()
            c["parent"] = best["type"]


def table_text(html):
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html or "", re.S)
    return "\n".join(" ".join(re.sub(r"<[^>]+>", "", c).strip() for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S))
                     for r in rows)


def main():
    out_dir = pathlib.Path(sys.argv[1])
    meta = json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8"))
    test_pdf = pymupdf.open(pathlib.Path(sys.argv[2]).with_name("test.pdf"))
    pages = []
    for m in meta:
        f = out_dir / "raw" / f"p{m['page']:04d}_res.json"
        if not f.exists():
            cands = list((out_dir / "raw").glob(f"p{m['page']:04d}_res*.json"))
            if not cands:
                print("missing", f); continue
            f = cands[0]
        r = json.loads(f.read_text(encoding="utf-8"))
        r = r.get("res", r)
        W_px, H_px = r["width"], r["height"]
        # display size in points (rotation applied) of the same page in the test PDF
        rect = test_pdf[m["sub_idx"]].rect
        Wpt, Hpt = rect.width, rect.height
        sx, sy = Wpt / W_px, Hpt / H_px
        ocr = r.get("overall_ocr_res") or {}
        lines = []
        for t, box in zip(ocr.get("rec_texts", []), ocr.get("rec_boxes", [])):
            if t.strip():
                lines.append({"bbox": [box[0] * sx, box[1] * sy, box[2] * sx, box[3] * sy], "text": t})
        blocks = []
        groups = {}
        for b in r.get("parsing_res_list", []):
            groups.setdefault(b.get("group_id", id(b)), []).append(b)
        for b in sorted(r.get("parsing_res_list", []), key=lambda b: b.get("block_order") if b.get("block_order") is not None else 1e9):
            lab = b["block_label"]
            t = LABEL.get(lab, "text")
            x0, y0, x1, y1 = b["block_bbox"]
            bb = [round(x0 * sx, 2), round(y0 * sy, 2), round(x1 * sx, 2), round(y1 * sy, 2)]
            content = clean_text(b.get("block_content") or "")
            members = groups.get(b.get("group_id", id(b)), [b])
            if lab in ("text", "list", "content") and not content.strip() and len(members) > 1:
                continue        # continuation part of a paragraph merged into its group head
            blk = {"type": t, "level": None, "bbox": bb, "text": "", "html": None, "lines": None, "image": None,
                   "tool_type": lab}
            if t == "title":
                blk["level"] = 1 if lab == "doc_title" else 2
                blk["text"] = re.sub(r"^#+\s*", "", content)
            elif t == "table":
                html = content if "<table" in content else None
                blk["html"] = html
                blk["text"] = table_text(html) if html else content
            elif t == "image":
                blk["text"] = ""          # never treat in-figure text as body text
                blk["vlm_text"] = content
                x0i, y0i, x1i, y1i = (int(v) for v in b["block_bbox"])
                crop = out_dir / "raw" / f"p{m['page']:04d}" / "imgs" / f"img_in_image_box_{x0i}_{y0i}_{x1i}_{y1i}.jpg"
                if crop.exists():
                    blk["image"] = str(crop.relative_to(out_dir)).replace("\\", "/")
            else:
                blk["text"] = content
                if len(members) > 1:
                    # paragraph spanning columns: keep every member box so the text layer can flow into them
                    blk["bboxes"] = [[round(m["block_bbox"][0] * sx, 2), round(m["block_bbox"][1] * sy, 2),
                                      round(m["block_bbox"][2] * sx, 2), round(m["block_bbox"][3] * sy, 2)]
                                     for m in sorted(members, key=lambda m: m.get("block_order") or 0)]
            if lines and t in ("text", "title", "list", "caption", "footnote", "toc"):
                inside = [l for l in lines if bb[0] - 2 <= (l["bbox"][0] + l["bbox"][2]) / 2 <= bb[2] + 2
                          and bb[1] - 2 <= (l["bbox"][1] + l["bbox"][3]) / 2 <= bb[3] + 2]
                if inside and re.sub(r"\s", "", "".join(l["text"] for l in inside)) == re.sub(r"\s", "", blk["text"]):
                    blk["lines"] = inside
            blocks.append(blk)
        attach_captions(blocks)
        pages.append({"page": m["page"], "w": Wpt, "h": Hpt, "blocks": blocks,
                      "render_px": [W_px, H_px], "render_dpi": round(W_px / (Wpt / 72))})
    tool = out_dir.parent.name
    doc = {"tool": tool, "has_continuation_flags": False, "pages": pages}
    (out_dir / "doc.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out_dir / "doc.json", len(pages), "pages", sum(len(p["blocks"]) for p in pages), "blocks")


if __name__ == "__main__":
    main()
