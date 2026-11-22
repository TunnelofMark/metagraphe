"""MinerU 4 middle_json.json -> normalized doc.json (see textlayer.py for the schema).

  python adapt_mineru.py <tool_out_dir> <pages.json>
  reads <tool_out_dir>/raw/middle_json.json, writes <tool_out_dir>/doc.json

- bbox: MinerU gives 0-1 fractions of the displayed page; converted to display-space points.
- page_idx (0-based in test.pdf) -> original page number via pages.json.
- Image blocks: the VLM's image description / in-image OCR is NOT kept as text (figures must
  not become body text); captions and footnotes become separate caption blocks linked to it.
"""
import re
import sys
import json
import pathlib
from textlayer import clean_equation, clean_html_equations, clean_text

TYPE_MAP = {"header": "header", "page_number": "page_number", "footer": "footer", "aside_text": "aside",
            "text": "text", "paragraph_title": "title", "doc_title": "title", "list": "list",
            "page_footnote": "footnote", "ref_text": "text", "equation": "text", "code": "text"}


def text_of(content):
    """Extract text from MinerU block content.
    - Block-level lists (items carry 'bbox', e.g. list items) are joined with '\\n'.
    - Inline span lists (e.g. 'text' + 'equation_inline' inside one paragraph) are joined inline
      without inserting spurious line breaks, and 'equation_inline' spans are de-LaTeXed directly.
    """
    if isinstance(content, str):
        return content
    if not content:
        return ""
    is_block_list = any(isinstance(c, dict) and "bbox" in c for c in content)
    out = []
    for c in content:
        if isinstance(c, str):
            out.append(c)
            continue
        ctype = c.get("type", "")
        inner = c.get("content", "")
        if isinstance(inner, str):
            piece = clean_equation(inner) if ctype in ("equation_inline", "equation") else inner
        else:
            piece = text_of(inner)
        if piece:
            out.append(piece)
    if is_block_list:
        return "\n".join(out)
    # Inline join: collapse excess whitespace MinerU puts right before/after inline equations
    joined = "".join(out)
    return re.sub(r" {2,}", " ", joined)


def main():
    out_dir = pathlib.Path(sys.argv[1])
    pages_meta = {i: p for i, p in enumerate(json.loads(pathlib.Path(sys.argv[2]).read_text(encoding="utf-8")))}
    mj = json.loads((out_dir / "raw" / "middle_json.json").read_text(encoding="utf-8"))
    dims = {p["page_idx"]: (p["width_pt"], p["height_pt"]) for p in mj["extensions"]["docvortex_layout"]["pages"]}

    pages = []
    for p in mj["pages"]:
        W, H = dims[p["page_idx"]]
        def pt(b):
            return [round(b[0] * W, 2), round(b[1] * H, 2), round(b[2] * W, 2), round(b[3] * H, 2)]
        blocks = []
        for b in p.get("blocks") or []:
            t = b["type"]
            if t in ("image", "table", "chart"):
                parts = b["content"]
                body = next((c for c in parts if c["type"].endswith("_body")), None)
                caps = [c for c in parts if c["type"].endswith("_caption")]
                notes = [c for c in parts if c["type"].endswith("_footnote")]
                kind = "table" if t == "table" else "image"
                blk = {"type": kind, "level": None, "bbox": pt(body["bbox"] if body else b["bbox"]),
                       "text": "", "html": None, "lines": None,
                       "image": f"raw/{body['image_path']}" if body and body.get("image_path") else None,
                       "caption": clean_text(" ".join(text_of(c["content"]).replace("\n", "") for c in caps)),
                       "footnote": clean_text(" ".join(text_of(c["content"]) for c in notes)),
                       "sub_type": b.get("sub_type")}
                if b.get("continues_prev"):
                    blk["continues_prev"] = True
                if kind == "table" and body:
                    blk["html"] = clean_text(clean_html_equations(body.get("content")))
                    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", blk["html"] or "", re.S)
                    blk["text"] = "\n".join(" ".join(re.sub(r"<[^>]+>", "", c).strip()
                                                     for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", r, re.S)) for r in rows)
                blocks.append(blk)
                for c in caps + notes:
                    blocks.append({"type": "caption", "level": None, "bbox": pt(c["bbox"]), "text": clean_text(text_of(c["content"])),
                                   "html": None, "lines": None, "image": None, "parent": kind})
                continue
            nt = TYPE_MAP.get(t, "text")
            raw_txt = text_of(b["content"])
            cleaned = clean_equation(raw_txt) if t == "equation" else clean_text(raw_txt)
            blk = {"type": nt, "level": (b.get("level") or 1) if nt == "title" else None, "bbox": pt(b["bbox"]),
                   "text": cleaned, "html": None, "lines": None, "image": None}
            if b.get("continues_prev"):
                blk["continues_prev"] = True
            if t != nt:
                blk["tool_type"] = t
            blocks.append(blk)
        meta = pages_meta[p["page_idx"]]
        pages.append({"page": meta["page"], "w": W, "h": H, "blocks": blocks})

    doc = {"tool": "mineru-" + mj["metadata"]["producer"]["version"], "has_continuation_flags": True, "pages": pages}
    (out_dir / "doc.json").write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    print(out_dir / "doc.json", len(pages), "pages", sum(len(p["blocks"]) for p in pages), "blocks")


if __name__ == "__main__":
    main()
